"""
scorer.py
---------
The scoring engine. Given a seed SongFeatures and a list of CandidateSong objects,
compute a weighted similarity score for each candidate and return a ranked top-N list.

Scoring dimensions:
  1. Audio similarity    (40%) — BPM proximity, key/mode match
  2. Genre / tag overlap (30%) — Jaccard similarity on tag sets
  3. Community signal    (20%) — Last.fm match score + MB tag strength
  4. Underground bonus   (10%) — rewards non-mainstream tracks

Each dimension is independently normalized to [0, 1] before weighting.
The final score is expressed as a percentage (0–100).
"""

import math
import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from app.fingerprint import SongFeatures
from app.candidates import CandidateSong

# ── Electronic genre taxonomy ─────────────────────────────────────────────────
#
# Genres are grouped into clusters of closely related styles. Within a cluster
# all genres are considered near-siblings. Between clusters, proximity decays
# according to CLUSTER_PROXIMITY — e.g. tech-house and minimal techno share
# rhythmic DNA, but tech-house and drum n' bass do not.

GENRE_CLUSTERS: dict[str, str] = {
    # Techno family
    "techno": "techno", "minimal techno": "techno", "minimal": "techno",
    "dub techno": "techno", "detroit techno": "techno", "industrial techno": "techno",
    "hard techno": "techno", "acid techno": "techno", "hypnotic techno": "techno",
    "raw techno": "techno",

    # House family
    "house": "house", "deep house": "house", "tech-house": "house",
    "tech house": "house", "microhouse": "house", "tribal house": "house",
    "afro house": "house", "soulful house": "house", "jackin house": "house",
    "chicago house": "house", "progressive house": "house", "funky house": "house",
    "acid house": "house", "vocal house": "house", "melodic house": "house",
    "lo-fi house": "house",

    # Drum n' bass / Jungle family
    "drum and bass": "dnb", "drum n bass": "dnb", "drum n' bass": "dnb",
    "dnb": "dnb", "d&b": "dnb", "jungle": "dnb", "liquid dnb": "dnb",
    "neurofunk": "dnb", "darkstep": "dnb", "jump up": "dnb",
    "halftime": "dnb", "rollers": "dnb",

    # Breakbeat family
    "breakbeat": "breakbeat", "breaks": "breakbeat", "nu skool breaks": "breakbeat",
    "broken beat": "breakbeat", "electro breaks": "breakbeat", "big beat": "breakbeat",

    # Ambient / Downtempo
    "ambient": "ambient", "ambient techno": "ambient", "ambient house": "ambient",
    "downtempo": "ambient", "chillout": "ambient", "trip-hop": "ambient",
    "trip hop": "ambient", "lo-fi": "ambient", "drone": "ambient",

    # Trance family
    "trance": "trance", "progressive trance": "trance", "psytrance": "trance",
    "psy trance": "trance", "goa trance": "trance", "uplifting trance": "trance",
    "tech trance": "trance",

    # IDM / Experimental
    "idm": "idm", "intelligent dance music": "idm", "glitch": "idm",
    "electronica": "idm", "experimental electronic": "idm", "clicks and cuts": "idm",
    "microsound": "idm", "braindance": "idm",

    # UK Bass / Garage
    "dubstep": "uk_bass", "uk garage": "uk_bass", "grime": "uk_bass",
    "bassline": "uk_bass", "2-step": "uk_bass", "2 step": "uk_bass",
    "post-dubstep": "uk_bass", "bass music": "uk_bass", "future garage": "uk_bass",

    # Electro / Synth
    "electro": "electro", "electro house": "electro", "electroclash": "electro",
    "synthwave": "electro", "italo disco": "electro", "new wave": "electro",
    "ebm": "electro", "industrial": "electro",
}

# Cross-cluster similarity (symmetric). Missing pairs default to 0.1 (unrelated).
# Values are additive bonuses on top of Jaccard — keep them in [0, 0.5].
CLUSTER_PROXIMITY: dict[tuple[str, str], float] = {
    ("techno", "house"):      0.35,   # share 4/4 kick, overlapping DJs
    ("techno", "idm"):        0.25,   # shared experimentalism
    ("techno", "ambient"):    0.20,   # dub techno bridge
    ("techno", "electro"):    0.20,
    ("techno", "trance"):     0.15,
    ("house", "uk_bass"):     0.20,   # garage / house crossover
    ("house", "electro"):     0.25,
    ("house", "ambient"):     0.15,
    ("house", "trance"):      0.20,
    ("dnb", "breakbeat"):     0.40,   # shared breakbeat heritage
    ("dnb", "uk_bass"):       0.30,   # bass-heavy, overlapping labels
    ("dnb", "idm"):           0.15,
    ("breakbeat", "uk_bass"): 0.25,
    ("breakbeat", "electro"): 0.20,
    ("ambient", "idm"):       0.30,
    ("trance", "electro"):    0.20,
    ("idm", "electro"):       0.20,
}

def _cluster_proximity(cluster_a: str, cluster_b: str) -> float:
    if cluster_a == cluster_b:
        return 0.5   # same cluster = strong intra-family bonus
    key = (min(cluster_a, cluster_b), max(cluster_a, cluster_b))
    return CLUSTER_PROXIMITY.get(key, 0.0)


# ── Tag normalization ─────────────────────────────────────────────────────────

TAG_ALIASES: dict[str, str] = {
    "drum n bass": "drum and bass",
    "drum n' bass": "drum and bass",
    "dnb": "drum and bass",
    "d&b": "drum and bass",
    "tech house": "tech-house",
    "minimal": "minimal techno",
    "psy trance": "psytrance",
    "trip hop": "trip-hop",
    "2 step": "2-step",
    "idm": "intelligent dance music",
    "ebm": "electronic body music",
}

def _normalize_tag(tag) -> str:
    # MB returns tags as {"name": "...", "count": N} dicts in some paths
    if isinstance(tag, dict):
        tag = tag.get("name", "")
    t = str(tag).strip().lower()
    return TAG_ALIASES.get(t, t)

logger = logging.getLogger(__name__)

# ── Scoring weights (must sum to 1.0) ────────────────────────────────────────

WEIGHTS = {
    "audio":       0.40,
    "genre":       0.40,
    "underground": 0.20,
}

# BPM tolerance: scores decay from 1.0 → 0.0 over this range (in BPM)
BPM_TOLERANCE = 30.0

# Key compatibility matrix (semitone distances that are "compatible")
# Unison = 0, Perfect 5th = 7, Relative minor/major = 3 or 9
KEY_COMPAT = {0: 1.0, 3: 0.8, 4: 0.7, 5: 0.7, 7: 0.9, 9: 0.8, 12: 0.95}

NOTE_TO_SEMITONE = {
    "C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3,
    "E": 4, "F": 5, "F#": 6, "Gb": 6, "G": 7, "G#": 8,
    "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11,
}


@dataclass
class ScoredResult:
    """A scored and ranked recommendation."""
    rank: int
    title: str
    artist: str
    match_pct: float            # 0–100, the headline number shown to users
    source: str
    source_url: Optional[str]
    youtube_id: Optional[str]
    is_underground: bool
    view_count: Optional[int]
    same_label: bool = False

    # Score breakdown for transparency / debugging
    score_audio: float = 0.0
    score_genre: float = 0.0
    score_underground: float = 0.0

    genre_tags: list = field(default_factory=list)
    mood_tags: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "title": self.title,
            "artist": self.artist,
            "match_pct": round(self.match_pct, 1),
            "source": self.source,
            "source_url": self.source_url,
            "youtube_id": self.youtube_id,
            "is_underground": self.is_underground,
            "view_count": self.view_count,
            "same_label": self.same_label,
            "breakdown": {
                "audio": round(self.score_audio * 100, 1),
                "genre": round(self.score_genre * 100, 1),
                "underground": round(self.score_underground * 100, 1),
            },
            "genre_tags": self.genre_tags,
            "mood_tags": self.mood_tags,
        }


def score_and_rank(
    seed: SongFeatures,
    candidates: list[CandidateSong],
    top_n: int = 10,
) -> list[ScoredResult]:
    """
    Score all candidates against the seed and return top_n ranked results.
    """
    if not candidates:
        return []

    scored = []
    for c in candidates:
        s_audio       = _score_audio(seed, c)
        s_genre       = _score_genre(seed, c)
        s_underground = c.underground_score

        weighted = (
            WEIGHTS["audio"]       * s_audio +
            WEIGHTS["genre"]       * s_genre +
            WEIGHTS["underground"] * s_underground
        )

        scored.append((weighted, s_audio, s_genre, s_underground, c))

    # Sort descending by weighted score
    scored.sort(key=lambda x: x[0], reverse=True)

    # Diversity filter: allow at most MAX_PER_ARTIST results per artist so a
    # single label or prolific artist doesn't flood the top 10. The highest-
    # scoring result(s) for each artist are kept; the rest are dropped.
    MAX_PER_ARTIST = 2
    artist_counts: dict[str, int] = {}
    diverse: list = []
    for entry in scored:
        # Normalise joint credits ("Artist A / Artist B", "A & B", "A, B")
        # so they don't bypass the per-artist cap
        raw = entry[4].artist.lower().strip()
        artist_key = re.split(r"[/,&]|\bfeat\b|\bvs\b", raw)[0].strip()
        if artist_counts.get(artist_key, 0) < MAX_PER_ARTIST:
            diverse.append(entry)
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
        if len(diverse) >= top_n:
            break

    results = []
    for rank, (weighted, s_audio, s_genre, s_under, c) in enumerate(diverse, 1):
        results.append(ScoredResult(
            rank=rank,
            title=c.title,
            artist=c.artist,
            match_pct=weighted * 100,
            source=c.source,
            source_url=c.source_url,
            youtube_id=c.youtube_id,
            is_underground=c.is_underground,
            view_count=c.view_count,
            same_label=c.same_label,
            score_audio=s_audio,
            score_genre=s_genre,
            score_underground=s_under,
            genre_tags=c.genre_tags,
            mood_tags=c.mood_tags,
        ))

    return results


# ── Individual dimension scorers ─────────────────────────────────────────────

def _score_audio(seed: SongFeatures, candidate: CandidateSong) -> float:
    """
    Audio similarity score [0, 1].
    Uses real BPM/key data when both seed and candidate have been enriched;
    falls back to 0.5 (neutral) when either side lacks audio features.
    """
    if not seed.has_audio_features or seed.bpm == 0:
        return 0.5

    if not candidate.has_audio_features or candidate.bpm == 0:
        return 0.5  # candidate not yet enriched — don't penalise

    bpm_score = _bpm_similarity(seed.bpm, candidate.bpm)
    key_score = _key_similarity(seed.key, seed.mode, candidate.key, candidate.mode)

    # Weight BPM slightly more than key — rhythm is a stronger similarity signal
    return bpm_score * 0.6 + key_score * 0.4


def _score_genre(seed: SongFeatures, candidate: CandidateSong) -> float:
    """
    Genre + tag similarity [0, 1] with electronic subgenre awareness.

    Three layers:
      1. Jaccard on normalized tags (exact overlap).
      2. Cluster proximity bonus — tech-house and microhouse score high even
         without a shared tag; tech-house and drum n' bass score low.
      3. Partial-string fallback for spelling variants not caught by normalization.
    """
    def collect_tags(genre_list, mood_list, raw_yt_tags):
        tags = set()
        for t in genre_list + mood_list:
            tags.add(_normalize_tag(t))
        for t in raw_yt_tags[:10]:
            tags.add(_normalize_tag(t))
        return tags

    seed_tags = collect_tags(seed.genre_tags, seed.mood_tags, [])
    cand_tags = collect_tags(
        candidate.genre_tags,
        candidate.mood_tags,
        candidate.raw_metadata.get("tags", []),
    )

    if not seed_tags and not cand_tags:
        return 0.5
    if not seed_tags or not cand_tags:
        return 0.3

    # 1. Jaccard
    intersection = seed_tags & cand_tags
    union = seed_tags | cand_tags
    jaccard = len(intersection) / len(union)

    # 2. Cluster proximity — best pairwise score across all (seed_tag, cand_tag) pairs
    cluster_bonus = 0.0
    for st in seed_tags:
        sc = GENRE_CLUSTERS.get(st)
        if not sc:
            continue
        for ct in cand_tags:
            cc = GENRE_CLUSTERS.get(ct)
            if not cc:
                continue
            cluster_bonus = max(cluster_bonus, _cluster_proximity(sc, cc))
    # Scale cluster bonus so it can contribute up to 0.4 on top of Jaccard
    cluster_bonus *= 0.4

    # 3. Partial-string fallback (catches e.g. "house" inside "deep house")
    partial_bonus = 0.0
    for st in seed_tags:
        for ct in cand_tags:
            if st != ct and (st in ct or ct in st):
                partial_bonus += 0.04
    partial_bonus = min(partial_bonus, 0.15)

    return min(1.0, jaccard + cluster_bonus + partial_bonus)



def _bpm_similarity(bpm_a: float, bpm_b: float) -> float:
    """
    Gaussian BPM similarity. Checks original, half-time, and double-time.
    Returns 1.0 for identical BPM, decays to ~0 at BPM_TOLERANCE distance.
    """
    if bpm_a <= 0 or bpm_b <= 0:
        return 0.5

    candidates_bpm = [bpm_b, bpm_b * 2, bpm_b / 2]
    best = max(
        math.exp(-((bpm_a - b) ** 2) / (2 * (BPM_TOLERANCE / 2) ** 2))
        for b in candidates_bpm
    )
    return best


def _key_similarity(key_a: str, mode_a: str, key_b: str, mode_b: str) -> float:
    """
    Key compatibility score [0, 1].
    Uses the circle of fifths and relative major/minor relationships.
    """
    if key_a == "unknown" or key_b == "unknown":
        return 0.5

    sa = NOTE_TO_SEMITONE.get(key_a, -1)
    sb = NOTE_TO_SEMITONE.get(key_b, -1)

    if sa == -1 or sb == -1:
        return 0.5

    # Semitone distance (circular)
    dist = min(abs(sa - sb), 12 - abs(sa - sb))

    # Same mode bonus
    mode_bonus = 0.1 if mode_a == mode_b else 0.0

    return min(1.0, KEY_COMPAT.get(dist, 0.3) + mode_bonus)

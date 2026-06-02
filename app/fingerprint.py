"""
fingerprint.py
--------------
Given a song title + artist, return a normalized feature vector.

Pipeline:
  1. MusicBrainz  → resolve to a canonical recording MBID
  2. AcousticBrainz → fetch low-level + high-level audio features
  3. Fallback      → estimate features from MusicBrainz tags alone

Both APIs are free and require no key.
"""

import httpx
import asyncio
import logging
from typing import Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

MUSICBRAINZ_BASE = "https://musicbrainz.org/ws/2"
ACOUSTICBRAINZ_BASE = "https://acousticbrainz.org"
USER_AGENT = "MusicRecommender/0.1 (dev-build)"

MB_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}


@dataclass
class SongFeatures:
    """Normalized feature vector for one song."""
    title: str
    artist: str
    mbid: Optional[str] = None

    # Audio features (all normalized 0.0–1.0 unless noted)
    bpm: float = 0.0            # raw BPM, not normalized
    bpm_confidence: float = 0.0
    key: str = "unknown"        # e.g. "C", "F#"
    mode: str = "unknown"       # "major" or "minor"
    energy: float = 0.0
    danceability: float = 0.0
    valence: float = 0.0        # musical positiveness
    acousticness: float = 0.0
    instrumentalness: float = 0.0

    # Genre / mood tags (list of strings)
    genre_tags: list = field(default_factory=list)
    mood_tags: list = field(default_factory=list)

    # Provenance flags
    has_audio_features: bool = False
    source: str = "unknown"     # "acousticbrainz" | "musicbrainz_tags" | "fallback"

    def to_dict(self):
        return asdict(self)


async def lookup_song(title: str, artist: str, client: httpx.AsyncClient) -> SongFeatures:
    """
    Main entry point. Returns a SongFeatures for the given title/artist.
    Tries AcousticBrainz first; falls back to tag-based estimation.
    """
    features = SongFeatures(title=title, artist=artist)

    # Step 1: resolve MBID via MusicBrainz
    mbid = await _resolve_mbid(title, artist, client)
    if mbid:
        features.mbid = mbid
        logger.info(f"Resolved MBID: {mbid}")

        # Step 2: fetch audio features from AcousticBrainz
        ab_features = await _fetch_acousticbrainz(mbid, client)
        if ab_features:
            features = _merge_ab_features(features, ab_features)
            features.has_audio_features = True
            features.source = "acousticbrainz"
            logger.info("AcousticBrainz features loaded")
        else:
            logger.info("AcousticBrainz miss — fetching MB tags")

        # Step 3: always fetch MB tags for genre/mood (supplement AB or be the fallback)
        mb_tags = await _fetch_mb_tags(mbid, client)
        features.genre_tags = mb_tags.get("genres", [])
        features.mood_tags = mb_tags.get("moods", [])

        if not features.has_audio_features:
            features.source = "musicbrainz_tags"

    else:
        logger.warning(f"Could not resolve MBID for '{title}' by '{artist}'")
        features.source = "fallback"

    return features


async def _resolve_mbid(title: str, artist: str, client: httpx.AsyncClient) -> Optional[str]:
    """Search MusicBrainz recordings and return the best-match MBID."""
    query = f'recording:"{title}" AND artist:"{artist}"'
    params = {"query": query, "limit": 5, "fmt": "json"}

    try:
        resp = await client.get(
            f"{MUSICBRAINZ_BASE}/recording",
            params=params,
            headers=MB_HEADERS,
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        recordings = data.get("recordings", [])
        if recordings:
            # Pick the highest-score result
            best = max(recordings, key=lambda r: r.get("score", 0))
            return best.get("id")
    except Exception as e:
        logger.error(f"MusicBrainz lookup failed: {e}")

    return None


async def _fetch_acousticbrainz(mbid: str, client: httpx.AsyncClient) -> Optional[dict]:
    """
    Fetch high-level and low-level features from AcousticBrainz.
    Note: AcousticBrainz was sunset in 2022 — this will return None for most tracks,
    but the structure is retained so we can swap in a replacement (e.g. Essentia online).
    """
    try:
        hl_resp = await client.get(
            f"{ACOUSTICBRAINZ_BASE}/{mbid}/high-level",
            timeout=8.0,
        )
        ll_resp = await client.get(
            f"{ACOUSTICBRAINZ_BASE}/{mbid}/low-level",
            timeout=8.0,
        )
        if hl_resp.status_code == 200 and ll_resp.status_code == 200:
            return {"high_level": hl_resp.json(), "low_level": ll_resp.json()}
    except Exception as e:
        logger.debug(f"AcousticBrainz fetch failed: {e}")

    return None


def _merge_ab_features(features: SongFeatures, ab: dict) -> SongFeatures:
    """Parse AcousticBrainz response into our SongFeatures fields."""
    hl = ab.get("high_level", {})
    ll = ab.get("low_level", {})

    # Low-level
    rhythm = ll.get("rhythm", {})
    tonal = ll.get("tonal", {})

    features.bpm = rhythm.get("bpm", 0.0)
    features.bpm_confidence = rhythm.get("bpm_confidence", 0.0)

    key_data = tonal.get("key_key", "unknown")
    scale_data = tonal.get("key_scale", "unknown")
    features.key = key_data
    features.mode = scale_data

    # High-level (probabilities 0–1)
    def prob(path: list, ab_dict: dict) -> float:
        """Traverse nested dict safely."""
        d = ab_dict
        for k in path:
            if not isinstance(d, dict):
                return 0.0
            d = d.get(k, {})
        if isinstance(d, dict):
            return d.get("probability", 0.0)
        return float(d) if d else 0.0

    features.danceability = prob(["highlevel", "danceability", "all", "danceable"], hl)
    features.valence = prob(["highlevel", "mood_happy", "all", "happy"], hl)
    features.acousticness = prob(["highlevel", "voice_instrumental", "all", "instrumental"], hl)
    features.instrumentalness = features.acousticness
    features.energy = prob(["highlevel", "mood_aggressive", "all", "aggressive"], hl)

    # Genre tags from high-level classifier
    genre_prob = hl.get("highlevel", {}).get("genre_tzanetakis", {}).get("all", {})
    if genre_prob:
        features.genre_tags = [k for k, v in genre_prob.items() if v > 0.2]

    return features


async def _fetch_mb_tags(mbid: str, client: httpx.AsyncClient) -> dict:
    """Fetch genre and mood tags from MusicBrainz recording."""
    try:
        resp = await client.get(
            f"{MUSICBRAINZ_BASE}/recording/{mbid}",
            params={"inc": "tags+genres", "fmt": "json"},
            headers=MB_HEADERS,
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()

        genres = [g["name"] for g in data.get("genres", []) if g.get("count", 0) > 0]
        tags = [t["name"] for t in data.get("tags", []) if t.get("count", 0) > 0]

        # Separate mood-like tags from genre tags heuristically
        mood_keywords = {"happy", "sad", "aggressive", "calm", "dark", "upbeat",
                         "melancholy", "energetic", "relaxing", "intense", "chill"}
        moods = [t for t in tags if any(m in t.lower() for m in mood_keywords)]
        extra_genres = [t for t in tags if t not in moods]

        return {
            "genres": list(set(genres + extra_genres))[:10],
            "moods": list(set(moods))[:5],
        }
    except Exception as e:
        logger.debug(f"MB tags fetch failed: {e}")
        return {"genres": [], "moods": []}


# ── Quick sanity test ────────────────────────────────────────────────────────

async def _test():
    async with httpx.AsyncClient() as client:
        features = await lookup_song("Paranoid Android", "Radiohead", client)
        print(f"\n{'='*50}")
        print(f"Title:   {features.title}")
        print(f"Artist:  {features.artist}")
        print(f"MBID:    {features.mbid}")
        print(f"Source:  {features.source}")
        print(f"BPM:     {features.bpm}")
        print(f"Key:     {features.key} {features.mode}")
        print(f"Energy:  {features.energy:.2f}")
        print(f"Genres:  {features.genre_tags}")
        print(f"Moods:   {features.mood_tags}")
        print(f"{'='*50}\n")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_test())

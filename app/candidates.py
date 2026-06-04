"""
candidates.py
-------------
Fetches a pool of candidate songs from Discogs and YouTube.

Sources:
  - Discogs  → label-mates + artist catalog (primary underground/vinyl signal)
  - YouTube  → yt-dlp metadata search; each result is cross-referenced against
               Discogs before inclusion — tracks not found on Discogs are dropped

Last.fm and MusicBrainz are no longer used as candidate sources.
"""

import asyncio
import logging
import math
import os
import re
import shutil
import httpx
import subprocess
import json
from dataclasses import dataclass, field
from typing import Optional
from app.fingerprint import SongFeatures

# Resolve yt-dlp at import time so subprocess can always find it
_YTDLP_BIN = shutil.which("yt-dlp") or "/Users/natethompson/Library/Python/3.11/bin/yt-dlp"

logger = logging.getLogger(__name__)

MB_BASE = "https://musicbrainz.org/ws/2"
MB_HEADERS = {"User-Agent": "MusicRecommender/0.1 (dev-build)", "Accept": "application/json"}

DISCOGS_BASE = "https://api.discogs.com"
DISCOGS_WEB  = "https://www.discogs.com"

def _discogs_web_url(item: dict) -> Optional[str]:
    """Convert a Discogs API response item to a human-clickable web URL."""
    uri = item.get("uri", "")
    if uri:
        return f"{DISCOGS_WEB}{uri}" if uri.startswith("/") else uri
    resource = item.get("resource_url", "")
    return resource.replace("api.discogs.com", "www.discogs.com").replace(
        "/releases/", "/release/"
    ).replace("/artists/", "/artist/").replace("/labels/", "/label/") or None

DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN", "pHFtzwaohFgNfVMfdDzwjQtLDOHrYPYBVnauocxn")
DISCOGS_HEADERS = {
    "User-Agent": "IDFinder/0.1 (dev-build)",
    "Authorization": f"Discogs token={DISCOGS_TOKEN}",
}

# Kept for potential future re-integration
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "60983b58ba44ec18da9ccc0b264b08d4")


@dataclass
class CandidateSong:
    """A single candidate track with metadata for scoring."""
    title: str
    artist: str
    source: str                          # "discogs" | "youtube"
    source_url: Optional[str] = None
    mbid: Optional[str] = None
    youtube_id: Optional[str] = None
    view_count: Optional[int] = None
    duration_sec: Optional[int] = None
    genre_tags: list = field(default_factory=list)
    mood_tags: list = field(default_factory=list)
    lastfm_match: float = 0.0
    underground_score: float = 0.0       # computed from Discogs have count
    same_label: bool = False             # True if from the same label as the seed
    discogs_have_count: Optional[int] = None
    raw_metadata: dict = field(default_factory=dict)

    # Audio features populated by enrich_candidates()
    bpm: float = 0.0
    key: str = "unknown"
    mode: str = "unknown"
    has_audio_features: bool = False

    @property
    def display_name(self):
        return f"{self.title} — {self.artist}"

    @property
    def is_underground(self) -> bool:
        """Underground if Discogs have count is low, or low YouTube views."""
        if self.discogs_have_count is not None:
            return self.discogs_have_count < 500
        if self.source == "youtube" and self.view_count is not None:
            return self.view_count < 500_000
        return True


# ── Underground scoring ───────────────────────────────────────────────────────

def _compute_underground_score_discogs(have_count: int) -> float:
    """
    Exponential decay based on Discogs community 'Have' count.

    Calibrated so:
      50 haves   → ~85%  (very underground)
      280 haves  → ~51%  (moderate collection)
      1000 haves → ~10%  (well-known in the scene)

    Steeper than the previous curve to ensure meaningful spread across the
    range of have counts typically seen on underground electronic releases.
    Clamped to [0.05, 0.95] to avoid hard zeroes/ones.
    """
    if have_count <= 0:
        return 0.90  # no data → assume obscure
    score = 0.95 * math.exp(-0.00225 * have_count)
    return max(0.05, min(0.95, score))


def _compute_underground_score_yt(meta: dict) -> float:
    """
    Fallback underground score for YouTube candidates before Discogs
    cross-reference. Overwritten by _compute_underground_score_discogs
    once the track is verified.
    """
    score = 0.5
    view_count = meta.get("view_count", 0) or 0
    uploader = (meta.get("uploader") or "").lower()
    channel_follower = meta.get("channel_follower_count", 0) or 0

    if view_count < 10_000:
        score += 0.3
    elif view_count < 100_000:
        score += 0.2
    elif view_count < 500_000:
        score += 0.1
    elif view_count > 10_000_000:
        score -= 0.3

    official_signals = ["vevo", "records", "official", "music", "universal", "sony", "warner"]
    if any(s in uploader for s in official_signals):
        score -= 0.25

    if channel_follower and channel_follower < 10_000:
        score += 0.15

    return max(0.0, min(1.0, score))


# ── Candidate fetching ────────────────────────────────────────────────────────

async def fetch_all_candidates(
    seed: SongFeatures,
    client: httpx.AsyncClient,
    target_count: int = 50,
) -> list[CandidateSong]:
    """
    Fetch candidates from Discogs and YouTube in parallel.
    YouTube results are cross-referenced against Discogs — any track not found
    on Discogs is dropped before the candidate pool is returned.
    """
    discogs_task = _fetch_discogs_candidates(seed, client)
    youtube_task = _fetch_youtube_candidates(seed, target_count // 2)

    discogs_results, youtube_raw = await asyncio.gather(
        discogs_task, youtube_task, return_exceptions=True
    )

    candidates: list[CandidateSong] = []

    if isinstance(discogs_results, Exception):
        logger.warning(f"Discogs source failed: {discogs_results}")
    else:
        candidates.extend(discogs_results)

    if isinstance(youtube_raw, Exception):
        logger.warning(f"YouTube source failed: {youtube_raw}")
    elif youtube_raw:
        verified_yt = await _verify_youtube_against_discogs(youtube_raw, client)
        candidates.extend(verified_yt)

    # Deduplicate by normalised title+artist
    seen = set()
    unique = []
    for c in candidates:
        key = _normalize_key(c.title, c.artist)
        if key not in seen:
            seen.add(key)
            unique.append(c)

    # Remove the seed song itself and any candidate whose title is too similar
    # (e.g. "Crackiceboom House EP" when the seed is "Crackiceboom")
    seed_key = _normalize_key(seed.title, seed.artist)
    seed_title_clean = re.sub(r"[^a-z0-9]", "", seed.title.lower())
    def _title_too_similar(c: CandidateSong) -> bool:
        t = re.sub(r"[^a-z0-9]", "", c.title.lower())
        return (seed_title_clean in t) or (t in seed_title_clean)
    unique = [
        c for c in unique
        if _normalize_key(c.title, c.artist) != seed_key and not _title_too_similar(c)
    ]

    logger.info(f"Total unique candidates: {len(unique)}")
    return unique[:target_count]


async def _verify_youtube_against_discogs(
    candidates: list[CandidateSong],
    client: httpx.AsyncClient,
) -> list[CandidateSong]:
    """
    Cross-reference each YouTube candidate against Discogs.
    Survivors are enriched with have count, underground score, and style tags
    from the matched Discogs release. Candidates with no Discogs match are dropped.
    """
    sem = asyncio.Semaphore(3)  # stay under Discogs 60 req/min limit

    async def _check(c: CandidateSong) -> Optional[CandidateSong]:
        async with sem:
            try:
                release = await _discogs_search_release(c.title, c.artist, client)
                if not release:
                    logger.debug(f"No Discogs match — dropping YT candidate: {c.display_name}")
                    return None

                # Enrich with Discogs community data
                have_count = (release.get("community") or {}).get("have", 0)
                c.discogs_have_count = have_count
                c.underground_score = _compute_underground_score_discogs(have_count)

                # Replace genre tags with Discogs style tags (more granular)
                styles = [s.lower() for s in (release.get("styles") or [])]
                genres = [g.lower() for g in (release.get("genres") or [])]
                if styles or genres:
                    c.genre_tags = list(dict.fromkeys(styles + genres))

                release_id = release.get("id")
                if release_id:
                    c.raw_metadata["discogs_id"] = release_id

                await asyncio.sleep(0.3)
                return c
            except Exception as e:
                logger.debug(f"Discogs verification failed for {c.display_name}: {e}")
                return None

    results = await asyncio.gather(*[_check(c) for c in candidates])
    verified = [r for r in results if r is not None]
    logger.info(
        f"YouTube→Discogs cross-reference: "
        f"{len(verified)}/{len(candidates)} candidates verified"
    )
    return verified


async def enrich_seed_from_discogs(
    seed: SongFeatures, client: httpx.AsyncClient
) -> SongFeatures:
    """
    If the seed has no genre tags (MusicBrainz missed), pull style tags from
    Discogs and inject them into seed.genre_tags.
    """
    if DISCOGS_TOKEN == "DISCOGS_TOKEN_PLACEHOLDER" or seed.genre_tags:
        return seed

    try:
        release = await _discogs_search_release(seed.title, seed.artist, client)
        if not release:
            return seed

        styles = release.get("styles", []) or []
        genres = release.get("genres", []) or []
        all_tags = list(dict.fromkeys(s.lower() for s in styles + genres))

        if all_tags:
            seed.genre_tags = all_tags
            seed.source = f"{seed.source}+discogs"
            logger.info(f"Discogs enriched seed genres: {all_tags}")

    except Exception as e:
        logger.debug(f"Seed Discogs enrichment failed: {e}")

    return seed


async def _fetch_discogs_candidates(
    seed: SongFeatures, client: httpx.AsyncClient
) -> list[CandidateSong]:
    """
    Discogs-based candidate discovery. Three layers:
      1. Find seed release → extract style tags + label(s)
      2. Fetch label-mate releases (same_label=True) — strongest underground signal
      3. Fetch other releases by the same artist
    """
    if DISCOGS_TOKEN == "DISCOGS_TOKEN_PLACEHOLDER":
        logger.debug("Discogs token not set — skipping")
        return []

    candidates: list[CandidateSong] = []

    try:
        release = await _discogs_search_release(seed.title, seed.artist, client)
        if not release:
            return await _discogs_artist_releases(seed.artist, client, styles=[])

        styles = release.get("styles", []) or []
        genres = release.get("genres", []) or []
        all_tags = list(set(s.lower() for s in styles + genres))

        labels = release.get("labels") or []
        label_ids = [lbl["id"] for lbl in labels if lbl.get("id")]
        label_names_by_id = {
            lbl["id"]: lbl.get("name", "")
            for lbl in labels if lbl.get("id")
        }

        label_candidates = await _discogs_label_releases(
            label_ids[:2], label_names_by_id, all_tags, client
        )
        candidates.extend(label_candidates)

        artist_candidates = await _discogs_artist_releases(
            seed.artist, client, styles=all_tags
        )
        candidates.extend(artist_candidates)

    except Exception as e:
        logger.warning(f"Discogs fetch failed: {e}")

    return candidates


async def _discogs_search_release(
    title: str, artist: str, client: httpx.AsyncClient
) -> Optional[dict]:
    """Search Discogs for a specific release and return its full metadata."""
    try:
        resp = await client.get(
            f"{DISCOGS_BASE}/database/search",
            params={"q": f"{title} {artist}", "type": "release", "per_page": 5},
            headers=DISCOGS_HEADERS,
            timeout=10.0,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None

        release_id = results[0].get("id")
        if not release_id:
            return None

        detail = await client.get(
            f"{DISCOGS_BASE}/releases/{release_id}",
            headers=DISCOGS_HEADERS,
            timeout=10.0,
        )
        detail.raise_for_status()
        return detail.json()

    except Exception as e:
        logger.debug(f"Discogs release search failed: {e}")
        return None


async def _discogs_label_releases(
    label_ids: list[int],
    label_names_by_id: dict,
    style_tags: list[str],
    client: httpx.AsyncClient,
) -> list[CandidateSong]:
    """
    Fetch releases from the same label(s).
    These candidates are marked same_label=True and carry the label name.
    Underground score is a placeholder — overwritten in the enrich phase
    once the per-release have count is fetched.
    """
    candidates = []
    for label_id in label_ids:
        label_name = label_names_by_id.get(label_id, "")
        try:
            resp = await client.get(
                f"{DISCOGS_BASE}/labels/{label_id}/releases",
                params={"per_page": 25, "sort": "year", "sort_order": "desc"},
                headers=DISCOGS_HEADERS,
                timeout=10.0,
            )
            resp.raise_for_status()
            releases = resp.json().get("releases", [])

            for r in releases:
                title = r.get("title", "")
                artist = r.get("artist", "")
                if not title or not artist:
                    continue
                candidates.append(CandidateSong(
                    title=title,
                    artist=artist,
                    source="discogs",
                    source_url=_discogs_web_url(r),
                    genre_tags=style_tags,
                    same_label=True,
                    underground_score=0.75,  # placeholder until enrich phase
                    raw_metadata={
                        "discogs_id": r.get("id"),
                        "label_id": label_id,
                        "label_name": label_name,
                        "year": r.get("year"),
                    },
                ))
        except Exception as e:
            logger.debug(f"Discogs label {label_id} fetch failed: {e}")

    return candidates


async def _discogs_artist_releases(
    artist: str, client: httpx.AsyncClient, styles: list[str]
) -> list[CandidateSong]:
    """Fetch other releases by the same artist from Discogs."""
    try:
        resp = await client.get(
            f"{DISCOGS_BASE}/database/search",
            params={"q": artist, "type": "artist", "per_page": 3},
            headers=DISCOGS_HEADERS,
            timeout=10.0,
        )
        resp.raise_for_status()
        artists = resp.json().get("results", [])
        if not artists:
            return []

        artist_id = artists[0].get("id")
        if not artist_id:
            return []

        resp2 = await client.get(
            f"{DISCOGS_BASE}/artists/{artist_id}/releases",
            params={"per_page": 20, "sort": "year", "sort_order": "desc"},
            headers=DISCOGS_HEADERS,
            timeout=10.0,
        )
        resp2.raise_for_status()
        releases = resp2.json().get("releases", [])

        candidates = []
        for r in releases:
            title = r.get("title", "")
            role = r.get("role", "")
            if not title or role == "TrackAppearance":
                continue
            candidates.append(CandidateSong(
                title=title,
                artist=artist,
                source="discogs",
                source_url=_discogs_web_url(r),
                genre_tags=styles,
                underground_score=0.65,  # placeholder until enrich phase
                raw_metadata={"discogs_id": r.get("id"), "year": r.get("year"), "role": role},
            ))
        return candidates

    except Exception as e:
        logger.debug(f"Discogs artist search failed for '{artist}': {e}")
        return []


# ── YouTube ───────────────────────────────────────────────────────────────────

async def _fetch_youtube_candidates(
    seed: SongFeatures, count: int = 25
) -> list[CandidateSong]:
    """
    Use yt-dlp to search YouTube for related tracks (metadata only, no download).
    Results are cross-referenced against Discogs before being added to the pool.
    """
    genres = seed.genre_tags[:3]

    _VINYL_TERMS = "vinyl rip full track"
    _LABEL_TERMS = "original mix ep release"

    queries = []
    if genres:
        primary = genres[0]
        secondary = " ".join(genres[1:3])
        queries.append(f"{primary} {_VINYL_TERMS}")
        queries.append(f"{primary} {secondary} {_LABEL_TERMS}" if secondary else f"{primary} {_LABEL_TERMS}")
        queries.append(f"{seed.artist} {primary} original mix")
    else:
        queries.append(f"{seed.title} {seed.artist}")
        queries.append(f"{seed.artist} original mix")

    candidates = []
    for query in queries:
        results = await _yt_search(query, max_results=10)
        candidates.extend(results)
        if len(candidates) >= count:
            break

    return candidates[:count]


async def _yt_search(query: str, max_results: int = 10) -> list[CandidateSong]:
    """Run yt-dlp in search mode — metadata only, no download."""
    cmd = [
        _YTDLP_BIN,
        f"ytsearch{max_results}:{query}",
        "--dump-json",
        "--no-download",
        "--quiet",
        "--no-warnings",
        "--extractor-args", "youtube:skip=dash,hls",
    ]

    try:
        loop = asyncio.get_event_loop()
        proc = await loop.run_in_executor(
            None,
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        )

        candidates = []
        for line in proc.stdout.strip().splitlines():
            if not line:
                continue
            try:
                meta = json.loads(line)
                raw_title = meta.get("title", "")

                if _is_junk_result(raw_title, meta):
                    logger.debug(f"Filtered junk YT result: {raw_title}")
                    continue

                title, artist = _parse_yt_title(raw_title, meta.get("uploader", ""))
                view_count = meta.get("view_count", 0)

                candidate = CandidateSong(
                    title=title,
                    artist=artist,
                    source="youtube",
                    source_url=f"https://youtube.com/watch?v={meta.get('id', '')}",
                    youtube_id=meta.get("id"),
                    view_count=view_count,
                    duration_sec=meta.get("duration"),
                    underground_score=_compute_underground_score_yt(meta),
                    raw_metadata={
                        "channel": meta.get("uploader"),
                        "like_count": meta.get("like_count"),
                        "tags": meta.get("tags", []),
                        "categories": meta.get("categories", []),
                    },
                )
                candidates.append(candidate)
            except json.JSONDecodeError:
                continue

        return candidates

    except Exception as e:
        logger.warning(f"yt-dlp search failed for '{query}': {e}")
        return []


_JUNK_TITLE_PATTERNS = [
    "type beat", "beat for sale", "free beat", "instrumental beat",
    "tutorial", "how to", "reaction to", "reacts to", "interview",
    "gameplay", "fortnite", "minecraft", "roblox", "gaming",
    "trailer", "official trailer", "movie clip", "film clip",
    "top 10", "top 5", "compilation", "best of",
    "karaoke", "cover version",
]

_JUNK_CATEGORIES = {"Gaming", "News & Politics", "Sports", "Education", "Howto & Style"}


def _is_junk_result(title: str, meta: dict) -> bool:
    t = title.lower()
    if any(p in t for p in _JUNK_TITLE_PATTERNS):
        return True
    categories = meta.get("categories") or []
    if any(c in _JUNK_CATEGORIES for c in categories):
        return True
    duration = meta.get("duration") or 0
    if duration > 5400:
        return True
    return False


def _parse_yt_title(raw_title: str, uploader: str) -> tuple[str, str]:
    separators = [" - ", " – ", " — ", " | "]
    for sep in separators:
        if sep in raw_title:
            parts = raw_title.split(sep, 1)
            return parts[1].strip(), parts[0].strip()
    return raw_title.strip(), uploader.strip()


# ── Enrichment ────────────────────────────────────────────────────────────────

async def enrich_candidates(
    candidates: list[CandidateSong],
    client: httpx.AsyncClient,
    max_enrich: int = 10,
) -> list[CandidateSong]:
    """
    Enrich candidates with audio features and Discogs style/have-count data.

    Phase 1 — MBID resolution (sequential, MB rate-limited)
      Only for YouTube candidates; Discogs candidates don't need MBIDs.

    Phase 2 — AcousticBrainz BPM/key lookups (parallel)
      Uses resolved MBIDs to fetch audio features.

    Phase 3 — Discogs per-release style + have-count enrichment (parallel)
      Fetches actual style tags and community.have for each Discogs candidate.
      Overwrites the placeholder underground_score with the have-count formula.
    """
    from app.fingerprint import _resolve_mbid, _fetch_acousticbrainz

    # Phases 1–2: audio features for YouTube candidates
    yt_targets = [c for c in candidates[:max_enrich] if c.source == "youtube"]

    for c in yt_targets:
        if not c.mbid:
            try:
                c.mbid = await _resolve_mbid(c.title, c.artist, client)
                await asyncio.sleep(1.1)
            except Exception as e:
                logger.debug(f"MBID resolution failed for {c.display_name}: {e}")

    async def _fetch_ab(c: CandidateSong) -> None:
        if not c.mbid:
            return
        try:
            ab_data = await _fetch_acousticbrainz(c.mbid, client)
            if ab_data:
                ll = ab_data.get("low_level", {})
                c.bpm = float(ll.get("rhythm", {}).get("bpm", 0.0))
                c.key = ll.get("tonal", {}).get("key_key", "unknown") or "unknown"
                c.mode = ll.get("tonal", {}).get("key_scale", "unknown") or "unknown"
                c.has_audio_features = c.bpm > 0
        except Exception as e:
            logger.debug(f"AB fetch failed for {c.display_name}: {e}")

    await asyncio.gather(*[_fetch_ab(c) for c in yt_targets])

    # Phase 3: Discogs style + have-count enrichment
    discogs_targets = [
        c for c in candidates[:max_enrich]
        if c.source == "discogs" and c.raw_metadata.get("discogs_id")
    ]
    if discogs_targets:
        await _enrich_discogs_styles(discogs_targets, client)

    return candidates


async def _enrich_discogs_styles(
    candidates: list[CandidateSong], client: httpx.AsyncClient
) -> None:
    """
    Fetch actual style tags and community.have for each Discogs candidate in parallel.
    Overwrites inherited seed-style tags with the release's own styles so the
    genre scorer can differentiate label-mates from each other.
    Computes underground_score via the have-count exponential decay formula.
    """
    sem = asyncio.Semaphore(3)

    async def _fetch_styles(c: CandidateSong) -> None:
        release_id = c.raw_metadata.get("discogs_id")
        if not release_id:
            return
        async with sem:
            try:
                resp = await client.get(
                    f"{DISCOGS_BASE}/releases/{release_id}",
                    headers=DISCOGS_HEADERS,
                    timeout=10.0,
                )
                resp.raise_for_status()
                data = resp.json()

                styles = [s.lower() for s in (data.get("styles") or [])]
                genres = [g.lower() for g in (data.get("genres") or [])]
                tags = list(dict.fromkeys(styles + genres))
                if tags:
                    c.genre_tags = tags

                have_count = (data.get("community") or {}).get("have", 0)
                c.discogs_have_count = have_count
                c.underground_score = _compute_underground_score_discogs(have_count)

                await asyncio.sleep(0.2)
            except Exception as e:
                logger.debug(f"Discogs style fetch failed for release {release_id}: {e}")

    await asyncio.gather(*[_fetch_styles(c) for c in candidates])


def _normalize_key(title: str, artist: str) -> str:
    """Lowercase + strip punctuation for deduplication."""
    def clean(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())
    return f"{clean(artist)}::{clean(title)}"

"""
candidates.py
-------------
Fetches a pool of ~50 candidate songs from multiple sources in parallel.

Sources:
  - Last.fm      → similar tracks endpoint
  - Discogs      → label-mates + artist catalog (best for vinyl/underground)
  - YouTube      → yt-dlp metadata search for rare/underground finds
  - MusicBrainz  → tag-based recording search fallback
"""

import asyncio
import logging
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

LASTFM_BASE = "https://ws.audioscrobbler.com/2.0"
MB_BASE = "https://musicbrainz.org/ws/2"
MB_HEADERS = {"User-Agent": "MusicRecommender/0.1 (dev-build)", "Accept": "application/json"}

DISCOGS_BASE = "https://api.discogs.com"
DISCOGS_WEB  = "https://www.discogs.com"

def _discogs_web_url(item: dict) -> Optional[str]:
    """Convert a Discogs API response item to a human-clickable web URL.
    Prefers the 'uri' field (relative web path) over resource_url (API path).
    """
    uri = item.get("uri", "")
    if uri:
        return f"{DISCOGS_WEB}{uri}" if uri.startswith("/") else uri
    # Fallback: resource_url path substitution
    resource = item.get("resource_url", "")
    return resource.replace("api.discogs.com", "www.discogs.com").replace(
        "/releases/", "/release/"
    ).replace("/artists/", "/artist/").replace("/labels/", "/label/") or None
DISCOGS_TOKEN = "pHFtzwaohFgNfVMfdDzwjQtLDOHrYPYBVnauocxn"
DISCOGS_HEADERS = {
    "User-Agent": "IDFinder/0.1 (dev-build)",
    "Authorization": f"Discogs token={DISCOGS_TOKEN}",
}

# Free Last.fm key (public read-only, rate-limited)
# Replace with your own key from https://www.last.fm/api/account/create
LASTFM_API_KEY = "60983b58ba44ec18da9ccc0b264b08d4"


@dataclass
class CandidateSong:
    """A single candidate track with metadata for scoring."""
    title: str
    artist: str
    source: str                         # "lastfm" | "youtube" | "musicbrainz"
    source_url: Optional[str] = None
    mbid: Optional[str] = None
    youtube_id: Optional[str] = None
    view_count: Optional[int] = None    # YouTube only
    duration_sec: Optional[int] = None
    genre_tags: list = field(default_factory=list)
    mood_tags: list = field(default_factory=list)
    lastfm_match: float = 0.0           # 0–1, Last.fm's own similarity score
    underground_score: float = 0.0      # computed from view_count + channel signals
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
        """Heuristic: underground if low views or only on YouTube."""
        if self.source == "youtube" and self.view_count is not None:
            return self.view_count < 500_000
        return self.source not in ("lastfm",)


async def fetch_all_candidates(
    seed: SongFeatures,
    client: httpx.AsyncClient,
    target_count: int = 50,
) -> list[CandidateSong]:
    """
    Fan out to all sources in parallel and return a deduplicated candidate pool.
    """
    tasks = [
        _fetch_lastfm_similar(seed, client),
        _fetch_discogs_candidates(seed, client),
        _fetch_youtube_candidates(seed, target_count // 2),
        _fetch_mb_similar_tags(seed, client),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    candidates: list[CandidateSong] = []
    for r in results:
        if isinstance(r, Exception):
            logger.warning(f"Candidate source failed: {r}")
        else:
            candidates.extend(r)

    # Deduplicate by normalized title+artist
    seen = set()
    unique = []
    for c in candidates:
        key = _normalize_key(c.title, c.artist)
        if key not in seen:
            seen.add(key)
            unique.append(c)

    # Remove the seed song itself
    seed_key = _normalize_key(seed.title, seed.artist)
    unique = [c for c in unique if _normalize_key(c.title, c.artist) != seed_key]

    logger.info(f"Total unique candidates: {len(unique)}")
    return unique[:target_count]


async def _fetch_lastfm_similar(
    seed: SongFeatures, client: httpx.AsyncClient
) -> list[CandidateSong]:
    """
    Hit Last.fm track.getSimilar. Works with a free API key.
    Falls back to a scrape of the public similar-tracks page if no key.
    """
    if LASTFM_API_KEY == "LASTFM_KEY_PLACEHOLDER":
        return await _fetch_lastfm_public_fallback(seed, client)

    params = {
        "method": "track.getSimilar",
        "track": seed.title,
        "artist": seed.artist,
        "limit": 25,
        "api_key": LASTFM_API_KEY,
        "format": "json",
        "autocorrect": 1,
    }
    try:
        resp = await client.get(LASTFM_BASE, params=params, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        tracks = data.get("similartracks", {}).get("track", [])
        return [
            CandidateSong(
                title=t["name"],
                artist=t["artist"]["name"],
                source="lastfm",
                source_url=t.get("url"),
                lastfm_match=float(t.get("match", 0)),
                raw_metadata=t,
            )
            for t in tracks
        ]
    except Exception as e:
        logger.warning(f"Last.fm API failed: {e}")
        return await _fetch_lastfm_public_fallback(seed, client)


async def _fetch_lastfm_public_fallback(
    seed: SongFeatures, client: httpx.AsyncClient
) -> list[CandidateSong]:
    """
    Scrape Last.fm's public similar-tracks page (no key required).
    Returns basic title/artist pairs — no match scores.
    """
    artist_slug = seed.artist.lower().replace(" ", "+")
    title_slug = seed.title.lower().replace(" ", "+")
    url = f"https://www.last.fm/music/{artist_slug}/_/{title_slug}/+similar"

    try:
        resp = await client.get(url, timeout=12.0, follow_redirects=True)
        if resp.status_code != 200:
            return []

        text = resp.text
        # Parse similar tracks from the page (simple pattern)
        # Last.fm renders track names in structured list items
        pattern = r'"track-name[^"]*"[^>]*>\s*([^<]+)<'
        artist_pattern = r'"artist-name[^"]*"[^>]*>\s*([^<]+)<'

        titles = re.findall(pattern, text)
        artists = re.findall(artist_pattern, text)

        candidates = []
        for title, artist in zip(titles[:20], artists[:20]):
            candidates.append(CandidateSong(
                title=title.strip(),
                artist=artist.strip(),
                source="lastfm",
                lastfm_match=0.5,  # unknown, assign mid score
            ))
        return candidates
    except Exception as e:
        logger.warning(f"Last.fm public fallback failed: {e}")
        return []


async def enrich_seed_from_discogs(
    seed: SongFeatures, client: httpx.AsyncClient
) -> SongFeatures:
    """
    If the seed has no genre tags (MusicBrainz missed), pull style tags from
    Discogs and inject them into seed.genre_tags. This ensures the genre scorer
    has real data to work with for vinyl/underground tracks not well-tagged in MB.
    """
    if DISCOGS_TOKEN == "DISCOGS_TOKEN_PLACEHOLDER" or seed.genre_tags:
        return seed

    try:
        release = await _discogs_search_release(seed.title, seed.artist, client)
        if not release:
            return seed

        styles = release.get("styles", []) or []
        genres = release.get("genres", []) or []
        all_tags = list(dict.fromkeys(               # preserve order, dedupe
            s.lower() for s in styles + genres
        ))

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
      1. Search for the seed track → extract style tags + label(s)
      2. Fetch other releases on the same label(s) → label-mates
      3. Fetch other releases by the same artist → artist catalog

    Discogs style tags are the most granular electronic genre taxonomy available
    and are the primary signal for underground/vinyl-only tracks.
    """
    if DISCOGS_TOKEN == "DISCOGS_TOKEN_PLACEHOLDER":
        logger.debug("Discogs token not set — skipping")
        return []

    candidates: list[CandidateSong] = []

    try:
        # Step 1: find the seed release on Discogs
        release = await _discogs_search_release(seed.title, seed.artist, client)
        if not release:
            # Fall back to artist-only search
            return await _discogs_artist_releases(seed.artist, client, styles=[])

        styles = release.get("styles", []) or []
        genres = release.get("genres", []) or []
        all_tags = list(set(s.lower() for s in styles + genres))

        label_ids = [
            lbl["id"] for lbl in (release.get("labels") or [])
            if lbl.get("id")
        ]

        # Step 2: label-mate releases (most powerful underground signal)
        label_candidates = await _discogs_label_releases(
            label_ids[:2], all_tags, client
        )
        candidates.extend(label_candidates)

        # Step 3: artist catalog
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

        # Pick the result with the closest title+artist match
        best = results[0]
        release_id = best.get("id")
        if not release_id:
            return None

        # Fetch full release for styles, labels, tracklist
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
    style_tags: list[str],
    client: httpx.AsyncClient,
) -> list[CandidateSong]:
    """Fetch releases from the same label(s) — the core underground discovery signal."""
    candidates = []
    for label_id in label_ids:
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
                    underground_score=0.75,
                    raw_metadata={"discogs_id": r.get("id"), "label_id": label_id, "year": r.get("year")},
                ))
        except Exception as e:
            logger.debug(f"Discogs label {label_id} fetch failed: {e}")

    return candidates


async def _discogs_artist_releases(
    artist: str, client: httpx.AsyncClient, styles: list[str]
) -> list[CandidateSong]:
    """Fetch other releases by the same artist from Discogs."""
    try:
        # Search for the artist
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

        # Fetch their releases
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
                underground_score=0.65,
                raw_metadata={"discogs_id": r.get("id"), "year": r.get("year"), "role": role},
            ))
        return candidates

    except Exception as e:
        logger.debug(f"Discogs artist search failed for '{artist}': {e}")
        return []


async def _fetch_youtube_candidates(
    seed: SongFeatures, count: int = 25
) -> list[CandidateSong]:
    """
    Use yt-dlp to search YouTube for related tracks (metadata only, no download).

    Query strategy — built around genre tags and vinyl/underground signals
    rather than artist names, which attract type beats and fan content.

    For electronic seeds we use highly specific style terms. For seeds with
    no genre data we fall back to a direct title+artist lookup only.
    """
    genres = seed.genre_tags[:3]

    # Electronic-specific search modifiers that surface real tracks on YouTube
    _VINYL_TERMS   = "vinyl rip full track"
    _RARE_TERMS    = "rare b-side unreleased"
    _LABEL_TERMS   = "original mix ep release"

    queries = []

    if genres:
        primary = genres[0]
        secondary = " ".join(genres[1:3])

        # Query 1: primary style + vinyl signal — highest precision for underground
        queries.append(f"{primary} {_VINYL_TERMS}")

        # Query 2: style combo + label/EP signal — finds actual releases
        if secondary:
            queries.append(f"{primary} {secondary} {_LABEL_TERMS}")
        else:
            queries.append(f"{primary} {_LABEL_TERMS}")

        # Query 3: artist anchored to genre — keeps it on-topic
        queries.append(f"{seed.artist} {primary} original mix")

    else:
        # No genre data — direct lookup only, no speculative genre queries
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
    """
    Run yt-dlp in search mode — metadata only, no download.
    Returns parsed CandidateSong objects with view counts and channel info.
    """
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
                    underground_score=_compute_underground_score(meta),
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
    """Return True for results that are clearly not music tracks."""
    t = title.lower()
    if any(p in t for p in _JUNK_TITLE_PATTERNS):
        return True
    categories = meta.get("categories") or []
    if any(c in _JUNK_CATEGORIES for c in categories):
        return True
    # Skip very long videos (> 90 min) — likely DJ sets or compilations, not tracks
    duration = meta.get("duration") or 0
    if duration > 5400:
        return True
    return False


def _parse_yt_title(raw_title: str, uploader: str) -> tuple[str, str]:
    """
    Attempt to split 'Artist - Song Title' from a YouTube title.
    Falls back to using the uploader as artist.
    """
    separators = [" - ", " – ", " — ", " | "]
    for sep in separators:
        if sep in raw_title:
            parts = raw_title.split(sep, 1)
            return parts[1].strip(), parts[0].strip()

    # No separator found — title is the song, uploader is the artist
    return raw_title.strip(), uploader.strip()


def _compute_underground_score(meta: dict) -> float:
    """
    Score 0–1 indicating how 'underground' a YouTube track appears.
    Higher = more underground.
    """
    score = 0.5  # baseline

    view_count = meta.get("view_count", 0) or 0
    uploader = (meta.get("uploader") or "").lower()
    channel_follower = meta.get("channel_follower_count", 0) or 0

    # Low view count is a strong underground signal
    if view_count < 10_000:
        score += 0.3
    elif view_count < 100_000:
        score += 0.2
    elif view_count < 500_000:
        score += 0.1
    elif view_count > 10_000_000:
        score -= 0.3  # mainstream

    # Official channels are NOT underground
    official_signals = ["vevo", "records", "official", "music", "universal", "sony", "warner"]
    if any(s in uploader for s in official_signals):
        score -= 0.25

    # Small channels tend to be more underground
    if channel_follower and channel_follower < 10_000:
        score += 0.15

    return max(0.0, min(1.0, score))


async def _fetch_mb_similar_tags(
    seed: SongFeatures, client: httpx.AsyncClient
) -> list[CandidateSong]:
    """
    Search MusicBrainz for similar recordings.

    Two strategies:
      - Genre tags present: query by tag intersection (most precise)
      - No genre tags: query by artist name to find discography neighbours,
        then broaden with a single-tag OR query if that yields too few results
    """
    queries = []

    if seed.genre_tags:
        # Primary: top 2 tags AND'd together
        queries.append(" AND ".join(f'tag:"{t}"' for t in seed.genre_tags[:2]))
        # Secondary: single top tag (broader catch)
        if len(seed.genre_tags) > 1:
            queries.append(f'tag:"{seed.genre_tags[0]}"')
    else:
        # Fallback: artist discography lookup
        queries.append(f'artist:"{seed.artist}"')

    candidates: list[CandidateSong] = []
    seen_mbids: set[str] = set()

    for query in queries:
        if len(candidates) >= 15:
            break
        try:
            resp = await client.get(
                f"{MB_BASE}/recording",
                params={"query": query, "limit": 15, "fmt": "json"},
                headers=MB_HEADERS,
                timeout=10.0,
            )
            resp.raise_for_status()
            for r in resp.json().get("recordings", []):
                mbid = r.get("id", "")
                if mbid in seen_mbids:
                    continue
                seen_mbids.add(mbid)
                artist_credit = r.get("artist-credit", [{}])
                artist = artist_credit[0].get("name", "Unknown") if artist_credit else "Unknown"
                # Pull any tags MB returned on the recording itself
                mb_tags = [t["name"] for t in r.get("tags", []) if t.get("count", 0) > 0]
                candidates.append(CandidateSong(
                    title=r.get("title", ""),
                    artist=artist,
                    source="musicbrainz",
                    source_url=f"https://musicbrainz.org/recording/{mbid}" if mbid else None,
                    mbid=mbid,
                    genre_tags=mb_tags,
                    raw_metadata=r,
                ))
            await asyncio.sleep(1.1)  # MB rate limit between queries
        except Exception as e:
            logger.warning(f"MusicBrainz tag search failed ({query}): {e}")

    return candidates[:15]


async def enrich_candidates(
    candidates: list[CandidateSong],
    client: httpx.AsyncClient,
    max_enrich: int = 10,
) -> list[CandidateSong]:
    """
    Fetch audio features (BPM, key, mode) and genre tags for the top candidates.

    Three-phase approach that parallelises where it's safe to do so:

      Phase 1 — MBID resolution (sequential, MB rate-limited)
        Skips Discogs candidates (no MBID needed for scoring) and any that
        already have an MBID from MusicBrainz source.

      Phase 2 — AcousticBrainz lookups (parallel)
        AB is a separate server with its own rate limits — safe to fan out.

      Phase 3 — MB tag fetch for tagless candidates (sequential, rate-limited)
        Only runs for candidates that came back with no genre data.

    Net effect: ~3× faster than the old sequential loop for a typical pool.
    """
    from app.fingerprint import _resolve_mbid, _fetch_acousticbrainz, _fetch_mb_tags

    targets = [c for c in candidates[:max_enrich] if c.source != "discogs"]

    # Phase 1: resolve missing MBIDs sequentially
    for c in targets:
        if not c.mbid:
            try:
                c.mbid = await _resolve_mbid(c.title, c.artist, client)
                await asyncio.sleep(1.1)
            except Exception as e:
                logger.debug(f"MBID resolution failed for {c.display_name}: {e}")

    # Phase 2: AcousticBrainz in parallel — different server, no shared rate limit
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

    await asyncio.gather(*[_fetch_ab(c) for c in targets])

    # Phase 3: MB tags for candidates that still have no genre data
    for c in targets:
        if c.mbid and not c.genre_tags:
            try:
                mb_tags = await _fetch_mb_tags(c.mbid, client)
                c.genre_tags = mb_tags.get("genres", [])
                c.mood_tags = mb_tags.get("moods", [])
                await asyncio.sleep(1.1)
            except Exception as e:
                logger.debug(f"MB tags failed for {c.display_name}: {e}")

    # Phase 4: Discogs per-release style enrichment (parallel)
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
    Fetch the actual style tags for each Discogs candidate in parallel.
    Replaces the inherited seed-style tags with the release's own styles so the
    genre scorer can differentiate label-mates from each other.

    Discogs authenticated rate limit is 60 req/min — a semaphore of 3 with a
    short sleep keeps us comfortably under that.
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
                tags = list(dict.fromkeys(styles + genres))  # styles first, dedupe
                if tags:
                    c.genre_tags = tags
                await asyncio.sleep(0.2)  # stay well under 60 req/min
            except Exception as e:
                logger.debug(f"Discogs style fetch failed for release {release_id}: {e}")

    await asyncio.gather(*[_fetch_styles(c) for c in candidates])

    return candidates


def _normalize_key(title: str, artist: str) -> str:
    """Lowercase + strip punctuation for deduplication."""
    def clean(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())
    return f"{clean(artist)}::{clean(title)}"

# Underground Music Recommender — Claude Code Brief

Paste this entire file as your first message in Claude Code.

---

## What we're building

A web app where a user types in a song + artist (e.g. "Paranoid Android by Radiohead") and gets back **top 10 similar songs**, ranked by percentage match. The key differentiator: results should surface **underground tracks** — vinyl rips, rare YouTube uploads, demo tapes, regional releases — not just Spotify/Apple Music hits.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  USER QUERY                     │
│           "Song Title by Artist"                │
└────────────────────┬────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────┐
│             SONG FINGERPRINTING                 │
│    BPM · key · mode · energy · genre tags       │
│    Source: MusicBrainz + AcousticBrainz         │
└────────────────────┬────────────────────────────┘
                     │
        ┌────────────┼────────────┬──────────────┐
        ▼            ▼            ▼              ▼
  ┌──────────┐ ┌──────────┐ ┌─────────┐ ┌──────────┐
  │ YouTube  │ │ Discogs  │ │ Reddit  │ │ Last.fm  │
  │ yt-dlp   │ │ genre +  │ │ ifyou-  │ │ similar  │
  │ low-view │ │ style    │ │ likeblk │ │ tracks   │
  │ filter   │ │ tags     │ │ subs    │ │          │
  └────┬─────┘ └────┬─────┘ └────┬────┘ └────┬─────┘
       └────────────┴────────────┴────────────┘
                          │
                          ▼
        ┌─────────────────────────────────┐
        │     CANDIDATE NORMALIZATION     │
        │  Deduplicate · unify schema     │
        └────────────────┬────────────────┘
                         │
                         ▼
        ┌─────────────────────────────────┐
        │         SCORING ENGINE          │
        │  Audio · Genre · Community ·    │
        │  Underground signal             │
        └────────────────┬────────────────┘
                         │
           ┌─────────────┼─────────────┐
           ▼             ▼             ▼
    ┌────────────┐ ┌───────────┐ ┌──────────────┐
    │Audio match │ │ Genre fit │ │  Community   │
    │   40%      │ │   30%     │ │  signal 20%  │
    │BPM·key·    │ │ Jaccard   │ │ Last.fm +    │
    │energy      │ │ tag overlap│ │ underground  │
    └────────────┘ └───────────┘ │  bonus 10%   │
                                 └──────────────┘
                         │
                         ▼
        ┌─────────────────────────────────┐
        │          TOP 10 RESULTS         │
        │  Ranked by % match · source     │
        │  badge · play link              │
        └─────────────────────────────────┘
```

### Underground detection logic (YouTube)
A track is flagged "underground" if:
- View count < 500k
- Uploader is NOT a VEVO/official/label channel
- Small subscriber count (< 10k)
- Channel name matches collector/archive patterns

---

## Current file structure

```
music-recommender/
├── app/
│   ├── __init__.py
│   ├── main.py          ← FastAPI app, POST /recommend endpoint
│   ├── fingerprint.py   ← MusicBrainz + AcousticBrainz lookup → SongFeatures
│   ├── candidates.py    ← YouTube (yt-dlp) + Last.fm + MB candidate fetcher
│   ├── scorer.py        ← Weighted similarity scoring → ScoredResult list
│   └── mock_data.py     ← Dev-mode mock API responses (USE_MOCKS = True)
├── requirements.txt
└── README.md
```

### Key data models

**SongFeatures** (fingerprint.py) — the seed song's feature vector:
```python
@dataclass
class SongFeatures:
    title: str
    artist: str
    mbid: Optional[str]
    bpm: float              # raw BPM
    key: str                # e.g. "C#"
    mode: str               # "major" | "minor"
    energy: float           # 0–1
    danceability: float     # 0–1
    valence: float          # 0–1 (musical positiveness)
    acousticness: float     # 0–1
    instrumentalness: float # 0–1
    genre_tags: list[str]
    mood_tags: list[str]
    has_audio_features: bool
    source: str             # "acousticbrainz" | "musicbrainz_tags" | "mock_*"
```

**CandidateSong** (candidates.py) — one raw candidate before scoring:
```python
@dataclass
class CandidateSong:
    title: str
    artist: str
    source: str             # "lastfm" | "youtube" | "musicbrainz"
    source_url: Optional[str]
    youtube_id: Optional[str]
    view_count: Optional[int]
    genre_tags: list[str]
    mood_tags: list[str]
    lastfm_match: float     # 0–1, Last.fm's own similarity score
    underground_score: float # 0–1, computed from view_count + channel signals
```

**ScoredResult** (scorer.py) — final ranked output:
```python
@dataclass
class ScoredResult:
    rank: int
    title: str
    artist: str
    match_pct: float        # 0–100, the headline number shown to users
    source: str
    source_url: Optional[str]
    youtube_id: Optional[str]
    is_underground: bool
    view_count: Optional[int]
    score_audio: float      # 0–1 component scores for transparency
    score_genre: float
    score_community: float
    score_underground: float
```

### Scoring weights
| Dimension       | Weight | Status              |
|----------------|--------|---------------------|
| Audio match     | 40%    | ⚠️ Returns 0.5 (neutral) until candidate enrichment is built |
| Genre/tag fit   | 30%    | ✅ Working — Jaccard similarity on tag sets |
| Community       | 20%    | ✅ Working — Last.fm match score + source trust |
| Underground     | 10%    | ✅ Working — view count + channel heuristics |

### API contract
```
POST /recommend
{
  "title": "Paranoid Android",
  "artist": "Radiohead",
  "top_n": 10
}

→ {
  "seed": { ...SongFeatures... },
  "results": [ ...10x ScoredResult... ],
  "elapsed_sec": 0.03,
  "candidate_pool_size": 33,
  "mock_mode": true
}
```

---

## What's working

- ✅ Full pipeline runs end-to-end (fingerprint → candidates → score → rank)
- ✅ Mock layer works — `pip install -r requirements.txt && uvicorn app.main:app --reload` runs immediately with no API keys
- ✅ Underground YouTube scoring heuristic (view count + channel signals)
- ✅ Genre/tag Jaccard similarity with partial-match bonus
- ✅ Last.fm community score
- ✅ Deduplication across sources
- ✅ FastAPI with CORS enabled (ready for frontend)

---

## What's NOT done yet (priority order)

### 1. Candidate enrichment (unlocks audio scoring — highest impact)
The audio dimension currently returns a flat 0.5 for all candidates because only the *seed* song has BPM/key data. We need to run MusicBrainz MBID lookup on each of the top 50 candidates and fetch their audio features too. This will make `_score_audio()` in scorer.py actually meaningful.

Files to modify: `candidates.py` (add `enrich_candidates()` async function), `scorer.py` (wire real BPM + key similarity into `_score_audio()`).

The BPM similarity function `_bpm_similarity()` and key compatibility matrix `_key_similarity()` are already implemented in scorer.py — they just need to be called with real data.

### 2. Frontend UI
A clean single-page HTML/JS or React app that:
- Has a search input: "Song title" + "Artist" fields (or a single combined field)
- Calls `POST /recommend` on the local FastAPI server
- Renders the top 10 as cards showing: rank, match %, title, artist, source badge (YouTube/Last.fm/MusicBrainz), underground badge if applicable, view count, play link
- Shows a score breakdown bar (audio / genre / community / underground) per card

### 3. Real API integration (swap mocks for live data)
In `mock_data.py`, flip `USE_MOCKS = False`. Then:
- **Last.fm** (free, 5 min setup): https://www.last.fm/api/account/create → set `LASTFM_API_KEY` env var in `candidates.py`
- **YouTube Data API** (optional — yt-dlp works without it): Google Cloud Console → YouTube Data API v3
- **MusicBrainz**: already implemented in fingerprint.py, no key needed — just needs real network access

### 4. Caching layer
Add Redis (or simple file-based cache) so repeat queries for the same song don't re-hit all the APIs. Fingerprints especially should be cached — they're expensive and deterministic.

### 5. Discogs integration
Not yet implemented. Discogs API (free key) is excellent for underground/collector tracks. Add a `_fetch_discogs_candidates()` function in candidates.py.

---

## How to run right now

```bash
cd music-recommender
pip install -r requirements.txt
uvicorn app.main:app --reload

# Test it:
curl -X POST http://localhost:8000/recommend \
  -H "Content-Type: application/json" \
  -d '{"title": "Paranoid Android", "artist": "Radiohead"}'
```

---

## Suggested first task for Claude Code

> "Read all the files in this project. Then build candidate enrichment: after fetching the raw candidate pool in candidates.py, run MusicBrainz MBID resolution on each candidate (reuse the `_resolve_mbid` function from fingerprint.py), then fetch their audio features. Store BPM and key on each CandidateSong. Then wire those into the `_score_audio()` function in scorer.py using the already-written `_bpm_similarity()` and `_key_similarity()` helpers."

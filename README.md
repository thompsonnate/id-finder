# Underground Music Recommender — Backend

## Quick start

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
# → http://localhost:8000
```

## Test the API

```bash
curl -X POST http://localhost:8000/recommend \
  -H "Content-Type: application/json" \
  -d '{"title": "Paranoid Android", "artist": "Radiohead"}'
```

## Project structure

```
app/
  main.py         ← FastAPI app, /recommend endpoint
  fingerprint.py  ← MusicBrainz + AcousticBrainz song lookup
  candidates.py   ← YouTube (yt-dlp) + Last.fm + MB candidate fetcher
  scorer.py       ← Weighted similarity scoring engine
  mock_data.py    ← Dev-mode mock responses (no API keys needed)
```

## Switching to production APIs

In `mock_data.py`, set `USE_MOCKS = False`.

Then set these env vars:
- `LASTFM_API_KEY` — free key from https://www.last.fm/api/account/create
- `YOUTUBE_API_KEY` — optional, yt-dlp works without one

## Scoring weights

| Dimension      | Weight | Notes                                  |
|---------------|--------|----------------------------------------|
| Audio match    | 40%    | BPM, key, energy (needs AB data)       |
| Genre/tag fit  | 30%    | Jaccard similarity on tag sets         |
| Community      | 20%    | Last.fm match score + source trust     |
| Underground    | 10%    | View count + channel signals           |

## Next steps

1. **Frontend** — React/HTML search UI consuming /recommend
2. **Candidate enrichment** — MBID-lookup per candidate for real audio scores
3. **Caching** — Redis layer to cache fingerprints (same song, instant repeat)
4. **Real API keys** — Last.fm (free), YouTube Data API (free tier)

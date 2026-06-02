"""
main.py
-------
FastAPI application. Single endpoint: POST /recommend

Request:  { "title": "...", "artist": "..." }
Response: { "seed": {...}, "results": [ top 10 ScoredResult dicts ] }
"""

import logging
import time
import os
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.fingerprint import lookup_song, SongFeatures
from app.candidates import fetch_all_candidates, enrich_candidates, enrich_seed_from_discogs, CandidateSong
from app.scorer import score_and_rank
from app.mock_data import USE_MOCKS, get_mock_seed, get_mock_candidates
from app.cache import (
    cache_get, cache_set,
    FINGERPRINT_TTL, CANDIDATES_TTL,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Underground Music Recommender",
    description="Find similar songs — including underground tracks not on Spotify.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class RecommendRequest(BaseModel):
    title: str
    artist: str
    top_n: int = 10


def _seed_from_dict(d: dict) -> SongFeatures:
    return SongFeatures(**{k: v for k, v in d.items() if k in SongFeatures.__dataclass_fields__})

def _candidate_to_dict(c: CandidateSong) -> dict:
    return {f: getattr(c, f) for f in CandidateSong.__dataclass_fields__}

def _candidate_from_dict(d: dict) -> CandidateSong:
    return CandidateSong(**{k: v for k, v in d.items() if k in CandidateSong.__dataclass_fields__})


@app.get("/")
async def root():
    """Serve the frontend."""
    frontend = os.path.join(os.path.dirname(__file__), "..", "index.html")
    if os.path.exists(frontend):
        return FileResponse(frontend)
    return {"status": "ok", "message": "ID Finder API", "mock_mode": USE_MOCKS}


@app.get("/health")
async def health():
    return {"status": "healthy", "mock_mode": USE_MOCKS}


@app.post("/cache/clear")
async def clear_cache():
    from app.cache import cache_clear
    removed = cache_clear()
    return {"removed": removed}


@app.post("/recommend")
async def recommend(req: RecommendRequest):
    if not req.title.strip() or not req.artist.strip():
        raise HTTPException(status_code=400, detail="title and artist are required")

    start = time.time()
    logger.info(f"Recommend: '{req.title}' by '{req.artist}' (mock={USE_MOCKS})")

    title = req.title.strip()
    artist = req.artist.strip()
    cache_key = f"{title.lower()}::{artist.lower()}"

    if USE_MOCKS:
        seed = get_mock_seed(title, artist)
        candidates = get_mock_candidates(seed)
    else:
        # Check fingerprint cache
        cached_seed = cache_get("fingerprint", cache_key, FINGERPRINT_TTL)
        cached_candidates = cache_get("candidates", cache_key, CANDIDATES_TTL)

        if cached_seed and cached_candidates:
            logger.info(f"Cache hit for '{title}' by '{artist}'")
            seed = _seed_from_dict(cached_seed)
            candidates = [_candidate_from_dict(c) for c in cached_candidates]
        else:
            async with httpx.AsyncClient() as client:
                seed = await lookup_song(title, artist, client)
                seed = await enrich_seed_from_discogs(seed, client)
                candidates = await fetch_all_candidates(seed, client, target_count=50)
                candidates = await enrich_candidates(candidates, client)
            cache_set("fingerprint", cache_key, seed.to_dict())
            cache_set("candidates", cache_key, [_candidate_to_dict(c) for c in candidates])

    results = score_and_rank(seed, candidates, top_n=req.top_n)

    elapsed = round(time.time() - start, 2)
    logger.info(f"Done in {elapsed}s — {len(candidates)} candidates → top {len(results)}")

    return {
        "seed": seed.to_dict(),
        "results": [r.to_dict() for r in results],
        "elapsed_sec": elapsed,
        "candidate_pool_size": len(candidates),
        "mock_mode": USE_MOCKS,
    }

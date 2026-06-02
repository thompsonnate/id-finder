"""
cache.py
--------
Simple file-based JSON cache with TTL.
Stores fingerprints and candidate lists so repeat queries skip the APIs.

Cache files live in .cache/ next to this file.
TTL defaults: fingerprints 30 days, candidates 7 days.
"""

import json
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Optional, Any

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).parent / ".cache"
_CACHE_DIR.mkdir(exist_ok=True)


def _key_path(namespace: str, key: str) -> Path:
    h = hashlib.md5(key.encode()).hexdigest()
    return _CACHE_DIR / f"{namespace}_{h}.json"


def cache_get(namespace: str, key: str, ttl_seconds: int) -> Optional[Any]:
    path = _key_path(namespace, key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if time.time() - data["ts"] > ttl_seconds:
            path.unlink(missing_ok=True)
            return None
        logger.debug(f"Cache hit: {namespace}/{key[:40]}")
        return data["value"]
    except Exception:
        return None


def cache_set(namespace: str, key: str, value: Any) -> None:
    path = _key_path(namespace, key)
    try:
        path.write_text(json.dumps({"ts": time.time(), "value": value}))
    except Exception as e:
        logger.warning(f"Cache write failed: {e}")


def cache_clear(namespace: Optional[str] = None) -> int:
    removed = 0
    for f in _CACHE_DIR.glob("*.json"):
        if namespace is None or f.name.startswith(namespace):
            f.unlink(missing_ok=True)
            removed += 1
    return removed


# TTLs
FINGERPRINT_TTL = 60 * 60 * 24 * 30   # 30 days — audio features don't change
CANDIDATES_TTL  = 60 * 60 * 24 * 7    # 7 days  — similar tracks lists are stable

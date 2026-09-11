"""On-disk cache with TTL for slow-changing reads (scrapes).

Avoids re-downloading the same pages on every command (and the 429s that come
with it). Never used for write actions or for data that must be fresh.

Serverless note: "on-disk" is now "wherever the backend keeps it". This matters
more than it looks — a Vercel function starts with an empty /tmp, so without a
shared cache EVERY tick would re-scrape futbolfantasy, which is both slow and
a good way to get rate-limited. Backed by Supabase, the cache is shared across
invocations and the scrape happens once per TTL for the whole deployment.
"""

import os

from . import config
from .storage import get_storage

CACHE_DIR = os.path.join(config.ROOT, ".cache")


def _path(key: str) -> str:
    """Legacy helper: where the local backend keeps this key."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
    return os.path.join(CACHE_DIR, safe + ".json")


def cached(key: str, ttl_seconds: int, producer):
    """Returns the cached value if fresh; otherwise calls producer and stores it.

    producer must return something JSON-serializable.
    """
    store = get_storage()
    try:
        hit = store.cache_get(key)
    except Exception:
        hit = None          # a cache that is down is a cache miss, never an error
    if hit is not None:
        return hit

    value = producer()
    try:
        store.cache_put(key, value, ttl_seconds)
    except Exception:
        pass                # failing to memoize is not a reason to fail the read
    return value


def clear():
    """Clears the entire cache."""
    get_storage().cache_clear()

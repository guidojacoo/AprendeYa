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


def cached(key: str, ttl_seconds: int, producer, default=None):
    """Returns the cached value if fresh; otherwise calls producer and stores it.

    producer must return something JSON-serializable.

    A producer that FAILS returns `default` rather than raising. This is what
    lets a run survive a source going down or running out of time: an agent that
    decides with one fewer signal still plays the gameweek, while one that raises
    out of its daily review does nothing at all — and the scraped sources are
    exactly the ones that fail (site redesigns, rate limits, a slow network
    against a function that is killed at 60 seconds).
    """
    store = get_storage()
    try:
        hit = store.cache_get(key)
    except Exception:
        hit = None          # a cache that is down is a cache miss, never an error
    if hit is not None:
        return hit

    try:
        value = producer()
    except Exception:
        return default
    try:
        store.cache_put(key, value, ttl_seconds)
    except Exception:
        pass                # failing to memoize is not a reason to fail the read
    return value


def clear():
    """Clears the entire cache."""
    get_storage().cache_clear()

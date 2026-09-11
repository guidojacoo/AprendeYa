"""Polite HTTP fetching: retries on 429 while honoring Retry-After.

Two courtesies, and only where they're owed:

- On 429 it waits (Retry-After or backoff) and retries. No artificial delay for
  anyone else, so urgent API actions stay fast.
- futbolfantasy gets a PACING floor: at most one request every THROTTLE seconds
  across ALL threads. The day the panel grew match pages and probable lineups,
  parallel workers hammered them into rate-limiting us (429s all over the
  console) — being scraped is a favor, and favors get queued politely.
"""

import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import config

THROTTLE_HOSTS = ("futbolfantasy.com",)
THROTTLE_SECONDS = 1.2
_pace_lock = threading.Lock()
_last_hit = {}


def _pace(url):
    """Blocks until this host's turn. Cheap no-op for everyone not throttled."""
    host = urlsplit(url).netloc.lower()
    if not any(host.endswith(h) for h in THROTTLE_HOSTS):
        return
    while True:
        with _pace_lock:
            ahora = time.monotonic()
            libre = _last_hit.get("ff", 0) + THROTTLE_SECONDS
            if ahora >= libre:
                _last_hit["ff"] = ahora
                return
            espera = libre - ahora
        time.sleep(espera)


def get(url: str, timeout: int = 20, retries: int = 3) -> str:
    """Fetches text. On 429, waits (Retry-After or backoff) and retries."""
    delay = 2
    for attempt in range(retries + 1):
        _pace(url)
        req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                retry_after = e.headers.get("Retry-After") or ""
                wait = int(retry_after) if retry_after.isdigit() else delay
                time.sleep(min(wait, 30))
                delay *= 2
                continue
            raise
    raise RuntimeError(f"No response after {retries} retries: {url}")

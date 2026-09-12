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

# A wall-clock deadline for every fetch in this process.
#
# Scraping is polite, which means slow: a 1.2s floor between requests and a cold
# cache can add up to more than the 60 seconds Vercel gives a function before it
# kills it — with an HTML error page and no chance to save anything. Past the
# deadline a fetch refuses immediately instead of starting a request it cannot
# finish, so the caller degrades (fewer sources, worse decisions) rather than
# the whole run dying.
_deadline = None


class DeadlineExceeded(TimeoutError):
    """The run is out of time; this fetch was not attempted."""


def set_deadline(monotonic_deadline):
    global _deadline
    _deadline = monotonic_deadline


def clear_deadline():
    global _deadline
    _deadline = None


def _remaining():
    return None if _deadline is None else _deadline - time.monotonic()


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
        left = _remaining()
        if left is not None:
            if left <= 1:
                raise DeadlineExceeded(f"out of time before fetching {url}")
            # Never let one request outlive the run that wants its answer.
            timeout = max(1, min(timeout, int(left)))
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

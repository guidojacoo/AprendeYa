"""Telling you when something happened — especially when something broke.

A bot you cannot see is a bot you cannot trust, and the three ways this one dies
are all silent: the LaLiga token expires, Supabase pauses itself after a week of
inactivity, GitHub disables the workflow after sixty days without a commit. None
of them raise anything anywhere you are looking. You find out when you open the
dashboard, which might be four gameweeks later.

So: a push when it matters, and only when it matters. Two things make that work.

DEDUPLICATION. A tick that fails every five minutes must not send 288 messages a
day — it sends one, and then stays quiet on that subject for a cooldown. The
cooldown is per SUBJECT, so a token expiring does not mute a failed clause.

BEST EFFORT. Nothing here can raise. A notifier that breaks the run it was meant
to report on is worse than no notifier: you would lose the bid AND the warning.

Telegram is the default because it is genuinely free, needs no server, and
delivers to a phone in a second. Any webhook (Discord, Slack) works too.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from . import config
from .storage import get_storage, parse_iso, to_iso, utcnow

TIMEOUT = 8

# How long to stay quiet about a subject after speaking about it, in seconds.
COOLDOWNS = {
    "tick_failed": 3600,        # a broken deploy: once an hour is plenty
    "token_expiring": 43200,    # twice a day as the 90 days run out
    "storage_down": 3600,
    "scraper_degraded": 21600,
    # A hole in the squad is a standing condition, not an event: it is still
    # there next review, and the review runs hourly. At the default cooldown
    # that is "Falta un POR" on your phone every fifteen minutes until the
    # market closes — the same message nine times, which is how a useful alert
    # becomes one you swipe away without reading. Once a day per position says
    # everything the ninth said.
    "gap": 86400,
    "default": 900,
}

# Events worth waking a phone for. Everything else goes to the dashboard only.
LEVELS = ("info", "good", "warn", "error")


def enabled():
    return bool((config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)
                or config.NOTIFY_WEBHOOK_URL)


def describe():
    return {"enabled": enabled(),
            "telegram": bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID),
            "webhook": bool(config.NOTIFY_WEBHOOK_URL)}


_ICON = {"info": "·", "good": "✅", "warn": "⚠️", "error": "🔴"}


def send(subject, text, level="info", force=False):
    """Notify once per subject per cooldown. Returns what it did, never raises.

    `subject` is the dedup key — make it stable for the same recurring problem
    ("tick_failed") and unique for a one-off worth repeating ("clause:p123").
    """
    if not enabled():
        return {"sent": False, "reason": "notifications are off"}
    try:
        if not force and _muted(subject):
            return {"sent": False, "reason": "cooldown", "subject": subject}
        body = f"{_ICON.get(level, '·')} {text}"
        ok = False
        if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
            ok = _telegram(body) or ok
        if config.NOTIFY_WEBHOOK_URL:
            ok = _webhook(subject, body, level) or ok
        if ok:
            _mark(subject)
        return {"sent": ok, "subject": subject, "level": level}
    except Exception as e:                       # noqa: BLE001
        # Deliberately swallowed. Losing the warning is bad; losing the run the
        # warning was about because the notifier threw is worse.
        return {"sent": False, "error": f"{type(e).__name__}: {e}"}


def _muted(subject):
    store = get_storage()
    seen = store.get_doc("notify_seen", {}) or {}
    last = parse_iso(seen.get(subject))
    if last is None:
        return False
    cooldown = COOLDOWNS.get(subject.split(":")[0], COOLDOWNS["default"])
    return (utcnow() - last).total_seconds() < cooldown


def _mark(subject):
    store = get_storage()
    seen = store.get_doc("notify_seen", {}) or {}
    seen[subject] = to_iso(utcnow())
    # Keep the newest few hundred so this document cannot grow forever.
    if len(seen) > 300:
        seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-300:])
    store.put_doc("notify_seen", seen)


def _post(url, payload, headers=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return 200 <= resp.status < 300


def _telegram(text):
    url = (f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
           f"/sendMessage")
    try:
        return _post(url, {"chat_id": config.TELEGRAM_CHAT_ID, "text": text,
                           "disable_web_page_preview": True})
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


def _webhook(subject, text, level):
    try:
        # `content` is what Discord reads; `text` is what Slack reads. Sending
        # both means one payload works for either without configuration.
        return _post(config.NOTIFY_WEBHOOK_URL,
                     {"content": text, "text": text,
                      "subject": subject, "level": level})
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


def clear(subject):
    """Forget a subject's cooldown, so recovery can be announced immediately."""
    try:
        store = get_storage()
        seen = store.get_doc("notify_seen", {}) or {}
        if seen.pop(subject, None) is not None:
            store.put_doc("notify_seen", seen)
    except Exception:
        pass

"""POST /api/admin — the manual controls.

Same secret as everything else. Every action here is one a human might want
between scheduled ticks: force a review, drop a queued bid, retune a cadence.

Deliberately absent: anything that spends money directly. There is no
"buy this player" endpoint, because an HTTP route that spends is a route that can
be replayed, and the whole design rests on actions being queued, keyed and
claimed rather than fired on request.
"""

import os
import sys

from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fantasybot import notify, scheduler, state, tick    # noqa: E402
from fantasybot.storage import get_storage, to_iso       # noqa: E402
from fantasybot.serverless.http import body, guarded, query  # noqa: E402

ACTIONS = ("review", "cancel", "setting", "settings", "tasks", "complete-task",
           "pending", "reset-cadence", "test-notify")


def _dispatch(handler):
    params = dict(query(handler))
    params.update(body(handler) or {})
    action = str(params.get("action") or "").strip().lower()
    store = get_storage()

    if action == "review":
        lines = []
        res = tick.run(mode="tick", force_review=True,
                       dry_run=str(params.get("dry_run", "")).lower() in ("1", "true"),
                       log=lines.append)
        res["log"] = lines[-40:]
        return (200 if res.get("ok") else 500), res

    if action == "test-notify":
        # End-to-end proof, which is the only kind worth having here: three
        # separate things (token, chat id, and having messaged the bot first)
        # must all be right, and getting any one wrong fails silently.
        if not notify.enabled():
            return 200, {"ok": False,
                         "error": "No hay notificaciones configuradas. Falta "
                                  "TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID (o "
                                  "NOTIFY_WEBHOOK_URL) en Vercel.",
                         "notify": notify.describe()}
        res = notify.send("test", "Prueba de FantasyBot: si leés esto, los "
                                  "avisos funcionan.", level="good", force=True)
        if not res.get("sent"):
            res["hint"] = ("Revisá: 1) el token completo con los dos puntos, "
                           "2) el chat id numérico, 3) que le hayas mandado "
                           "/start a TU bot — Telegram no deja que un bot "
                           "escriba primero.")
        return 200, {"ok": bool(res.get("sent")), "result": res,
                     "notify": notify.describe()}

    if action == "cancel":
        key = params.get("key")
        if not key:
            return 400, {"ok": False, "error": "cancel needs a `key` "
                                               "(the action's idempotency_key)"}
        return 200, {"ok": True, "cancelled": scheduler.cancel(key), "key": key}

    if action == "pending":
        return 200, {"ok": True, "pending": store.pending_actions(limit=100),
                     "next_deadline": to_iso(scheduler.next_deadline())}

    if action == "settings":
        return 200, {"ok": True, "settings": store.get_settings()}

    if action == "setting":
        key, value = params.get("key"), params.get("value")
        if not key:
            return 400, {"ok": False, "error": "setting needs `key` and `value`"}
        store.set_setting(key, value)
        return 200, {"ok": True, "settings": store.get_settings()}

    if action == "reset-cadence":
        # Makes the next tick review immediately instead of waiting out the
        # interval — handy right after a deploy.
        store.delete_doc("last_review_at")
        return 200, {"ok": True, "reset": "last_review_at"}

    if action == "tasks":
        return 200, {"ok": True, "tasks": state.pending_tasks()}

    if action == "complete-task":
        try:
            task_id = int(params.get("id"))
        except (TypeError, ValueError):
            return 400, {"ok": False, "error": "complete-task needs a numeric `id`"}
        state.complete_task(task_id)
        return 200, {"ok": True, "completed": task_id}

    return 400, {"ok": False, "error": f"unknown action {action!r}",
                 "actions": list(ACTIONS)}


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        guarded(self, lambda: _dispatch(self))

    def do_GET(self):
        guarded(self, lambda: _dispatch(self))

    def log_message(self, fmt, *args):
        pass

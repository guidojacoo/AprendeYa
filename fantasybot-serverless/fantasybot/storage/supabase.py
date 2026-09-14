"""Supabase backend: the same state, in PostgreSQL, over PostgREST.

Why REST and not a driver: fantasybot is stdlib-only, and that is worth keeping.
A Vercel Python function that needs no wheels is a function that cannot fail to
build, cold-starts faster, and has no connection pool to exhaust — which matters
a great deal when every tick is a brand-new process. Supabase exposes every table
over PostgREST, so `urllib` is genuinely enough.

Atomicity is done in the database, never in Python:

  * claiming an action is one `UPDATE ... WHERE id = ? AND status = 'pending'`
    that returns the row it changed. Two ticks race; exactly one gets a row back.
  * scheduling is an INSERT against a UNIQUE (scope, idempotency_key). The
    second insert loses with a 409, and we return the row already there instead
    of queueing a duplicate bid.
  * the tick mutex is the same trick: INSERT, and on conflict an UPDATE gated on
    the lease having actually expired.

Nothing here trusts wall-clock ordering between two functions.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from .. import config
from .base import (CANCELLED, PENDING, RUNNING, Storage, StorageError,
                   StorageUnavailable, parse_iso, to_iso, utcnow)

# A single request's patience. Three attempts at fifteen seconds is
# forty-five — more than the whole tick budget — so a slow database used to
# take the function down with it rather than being given up on.
TIMEOUT = 8
RETRIES = 2
# Total wall clock any one call may spend, retries and backoff included. The
# tick has about fifty seconds to live and this is one read inside it.
RETRY_BUDGET = 20
# A gateway timeout means the far side is busy. Coming back half a second later
# asks the same busy thing the same question; these are seconds, and they grow.
BACKOFF = (1.5, 4.0)


def _project_url(url):
    """The project base, whichever URL you pasted.

    Supabase shows you a bare "Project URL" and, elsewhere, the RESTful endpoint
    with `/rest/v1` already on it. Both look like the right thing to copy, and
    pasting the second one produces `/rest/v1/rest/v1/...` and a 404 on every
    single table — which reads exactly like "the migration never ran". Cheaper to
    accept both than to make anyone debug that.
    """
    url = (url or "").strip().rstrip("/")
    for suffix in ("/rest/v1", "/rest"):
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    return url


class SupabaseStorage(Storage):
    kind = "supabase"

    def __init__(self, url=None, key=None, scope=None):
        self.url = _project_url(url or config.SUPABASE_URL)
        self.key = key or config.SUPABASE_SERVICE_ROLE_KEY
        self.scope = scope or config.STORAGE_SCOPE
        if not self.url or not self.key:
            missing = [n for n, v in (("SUPABASE_URL", self.url),
                                      ("SUPABASE_SERVICE_ROLE_KEY", self.key))
                       if not v]
            if config.DOTENV_FOUND:
                where = (f"Read {config.DOTENV_PATH} and it set: "
                         f"{', '.join(config.DOTENV_KEYS) or '(nothing)'}.")
            else:
                where = (f"No .env at {config.DOTENV_PATH} — that is where it is "
                         f"looked for. On Windows check the real filename with "
                         f"`dir /a` or `Get-ChildItem -Force`: an editor that "
                         f"saved it as '.env.txt' looks like '.env' in Explorer.")
            raise StorageError(
                f"Supabase storage needs {' and '.join(missing)}. {where} "
                f"Blank lines like `KEY=` are skipped on purpose, so a variable "
                f"left empty counts as unset. Set them in the environment or in "
                f"that file, or set FANTASYBOT_STORAGE=local to use JSON files.")
        self.rest = f"{self.url}/rest/v1"

    # --- HTTP ----------------------------------------------------------------
    # Characters PostgREST needs to read literally in a query string.
    #
    # `+` is deliberately NOT here. An ISO timestamp carries its UTC offset as
    # "+00:00", and a literal `+` in a query string decodes to a SPACE — so every
    # timestamp filter reached Postgres as "2026-09-12T00:21:08.371615 00:00" and
    # was rejected as invalid syntax. That broke due_actions, claim_action and
    # pending_actions: the entire queue, silently, only against Supabase.
    SAFE_CHARS = "().,*:-"

    def _build_url(self, path, params=None):
        url = f"{self.rest}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, safe=self.SAFE_CHARS)
        return url

    def _request(self, method, path, params=None, body=None, prefer=None):
        url = self._build_url(path, params)
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if prefer:
            headers["Prefer"] = prefer

        last = None
        started = time.monotonic()

        def _spent():
            return time.monotonic() - started

        for attempt in range(RETRIES + 1):
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw.strip() else None
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:400]
                if e.code == 409:
                    # Unique violation. The caller decides what that means; it is
                    # a normal outcome here, not a failure.
                    raise ConflictError(detail) from None
                # 5xx is worth another go; a 4xx is our own bad request.
                if e.code >= 500:
                    last = StorageUnavailable(
                        f"{method} {path} -> {e.code}: {detail}")
                    wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                    if attempt < RETRIES and _spent() + wait < RETRY_BUDGET:
                        time.sleep(wait)
                        continue
                    raise last from e
                raise StorageError(f"{method} {path} -> {e.code}: {detail}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                # Every write below is either idempotent or unique-key guarded,
                # so a retry cannot duplicate anything.
                last = StorageUnavailable(f"{method} {path}: {e}")
                wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                if attempt < RETRIES and _spent() + wait < RETRY_BUDGET:
                    time.sleep(wait)
                    continue
                raise last from e
        raise last or StorageUnavailable(f"{method} {path} failed")

    def _select(self, table, params, limit=None, order=None):
        p = {"select": "*"}
        p.update(params)
        p["scope"] = f"eq.{self.scope}"
        if order:
            p["order"] = order
        if limit:
            p["limit"] = str(limit)
        return self._request("GET", table, params=p) or []

    def _upsert(self, table, row, on_conflict):
        return self._request(
            "POST", table, params={"on_conflict": on_conflict}, body=row,
            prefer="resolution=merge-duplicates,return=representation")

    # --- documents -----------------------------------------------------------
    def get_doc(self, name, default=None):
        rows = self._select("agent_state", {"key": f"eq.{name}"}, limit=1)
        if not rows:
            return default
        value = rows[0].get("value")
        return default if value is None else value

    def put_doc(self, name, value):
        self._upsert("agent_state",
                     {"scope": self.scope, "key": name, "value": value,
                      "updated_at": to_iso(utcnow())},
                     on_conflict="scope,key")

    def delete_doc(self, name):
        self._request("DELETE", "agent_state",
                      params={"scope": f"eq.{self.scope}", "key": f"eq.{name}"})

    # --- TTL cache -----------------------------------------------------------
    def cache_get(self, key):
        rows = self._select("cache_entries", {"key": f"eq.{key}"}, limit=1)
        if not rows:
            return None
        expires = parse_iso(rows[0].get("expires_at"))
        if expires is None or expires <= utcnow():
            return None
        return rows[0].get("value")

    def cache_put(self, key, value, ttl_seconds):
        self._upsert("cache_entries",
                     {"scope": self.scope, "key": key, "value": value,
                      "expires_at": to_iso(utcnow() + timedelta(seconds=ttl_seconds))},
                     on_conflict="scope,key")

    def cache_clear(self):
        self._request("DELETE", "cache_entries",
                      params={"scope": f"eq.{self.scope}"})

    # --- events --------------------------------------------------------------
    def emit_event(self, event):
        try:
            self._request("POST", "events", body={
                "scope": self.scope,
                "ts": to_iso(event.get("iso")) or to_iso(utcnow()),
                "run_id": event.get("run"),
                "kind": event.get("kind"),
                "title": event.get("title"),
                "status": event.get("status", "ok"),
                "detail": event.get("detail"),
            }, prefer="return=minimal")
        except StorageError:
            pass   # telemetry must never break an action
        return event

    def load_events(self, limit=500):
        rows = self._select("events", {}, limit=limit, order="ts.desc,id.desc")
        out = []
        for r in reversed(rows):
            out.append({"iso": r.get("ts"), "run": r.get("run_id"),
                        "kind": r.get("kind"), "title": r.get("title"),
                        "status": r.get("status"), "detail": r.get("detail"),
                        "ts": (parse_iso(r.get("ts")).timestamp()
                               if parse_iso(r.get("ts")) else None)})
        return out

    # --- market snapshots ----------------------------------------------------
    def get_market_snapshot(self, day_iso):
        rows = self._select("market_snapshots", {"day": f"eq.{day_iso}"}, limit=1)
        if not rows:
            return {}
        values = rows[0].get("player_values")
        return values if isinstance(values, dict) else {}

    def put_market_snapshot(self, day_iso, values, keep_days=40):
        self._upsert("market_snapshots",
                     {"scope": self.scope, "day": day_iso,
                      "player_values": values},
                     on_conflict="scope,day")
        from datetime import date
        try:
            cutoff = date.fromisoformat(day_iso) - timedelta(days=keep_days)
        except ValueError:
            return
        self._request("DELETE", "market_snapshots",
                      params={"scope": f"eq.{self.scope}",
                              "day": f"lt.{cutoff.isoformat()}"})

    # --- scheduled actions ---------------------------------------------------
    def schedule_action(self, action_type, payload, execute_at, idempotency_key,
                        expires_at=None):
        row = {
            "scope": self.scope,
            "type": action_type,
            "payload": payload,
            "execute_at": to_iso(execute_at),
            "expires_at": to_iso(expires_at),
            "idempotency_key": idempotency_key,
            "status": PENDING,
        }
        try:
            created = self._request("POST", "scheduled_actions", body=row,
                                    prefer="return=representation")
            return (created or [{}])[0]
        except ConflictError:
            pass
        # Already queued or already executed. Re-time it only while it is still
        # pending: a finished action must stay finished, or a re-plan would bid
        # a second time for a player we already bought.
        updated = self._request(
            "PATCH", "scheduled_actions",
            params={"scope": f"eq.{self.scope}",
                    "idempotency_key": f"eq.{idempotency_key}",
                    "status": f"eq.{PENDING}"},
            body={"execute_at": to_iso(execute_at), "payload": payload,
                  "expires_at": to_iso(expires_at)},
            prefer="return=representation")
        if updated:
            return updated[0]
        existing = self._select("scheduled_actions",
                                {"idempotency_key": f"eq.{idempotency_key}"}, limit=1)
        return existing[0] if existing else row

    def _due_filter(self, now):
        """Pending, or running with an expired lease (the tick that held it died)."""
        iso = to_iso(now)
        return {"execute_at": f"lte.{iso}",
                "or": f"(status.eq.{PENDING},and(status.eq.{RUNNING},locked_until.lt.{iso}))"}

    def due_actions(self, now=None, limit=25):
        now = now or utcnow()
        return self._select("scheduled_actions", self._due_filter(now),
                            limit=limit, order="execute_at.asc")

    def pending_actions(self, limit=50):
        return self._select("scheduled_actions",
                            {"status": f"in.({PENDING},{RUNNING})"},
                            limit=limit, order="execute_at.asc")

    def claim_action(self, action, lease_seconds=120):
        """One UPDATE, gated on the row still being claimable. If PostgREST hands
        a row back we own it; if it hands back nothing, another tick won the race
        and we must not touch this action."""
        now = utcnow()
        iso = to_iso(now)
        updated = self._request(
            "PATCH", "scheduled_actions",
            params={"id": f"eq.{action['id']}", "scope": f"eq.{self.scope}",
                    "or": f"(status.eq.{PENDING},and(status.eq.{RUNNING},locked_until.lt.{iso}))"},
            body={"status": RUNNING,
                  "locked_until": to_iso(now + timedelta(seconds=lease_seconds)),
                  "attempts": (action.get("attempts") or 0) + 1,
                  "updated_at": iso},
            prefer="return=representation")
        if not updated:
            return False
        action.update(updated[0])
        return True

    def finish_action(self, action, status, result=None, error=None):
        self._request(
            "PATCH", "scheduled_actions",
            params={"id": f"eq.{action['id']}", "scope": f"eq.{self.scope}"},
            body={"status": status, "result": result, "last_error": error,
                  "locked_until": None, "finished_at": to_iso(utcnow()),
                  "updated_at": to_iso(utcnow())},
            prefer="return=minimal")

    def cancel_action(self, idempotency_key):
        updated = self._request(
            "PATCH", "scheduled_actions",
            params={"scope": f"eq.{self.scope}",
                    "idempotency_key": f"eq.{idempotency_key}",
                    "status": f"eq.{PENDING}"},
            body={"status": CANCELLED, "updated_at": to_iso(utcnow())},
            prefer="return=representation")
        return bool(updated)

    def next_deadline(self):
        rows = self._select("scheduled_actions", {"status": f"eq.{PENDING}"},
                            limit=1, order="execute_at.asc")
        return parse_iso(rows[0]["execute_at"]) if rows else None

    # --- executions ----------------------------------------------------------
    def start_execution(self, trigger):
        rows = self._request("POST", "executions", body={
            "scope": self.scope, "trigger": trigger, "status": RUNNING,
            "started_at": to_iso(utcnow())}, prefer="return=representation")
        return (rows or [{}])[0].get("id")

    def finish_execution(self, execution_id, status, summary=None, error=None):
        if execution_id is None:
            return
        self._request("PATCH", "executions",
                      params={"id": f"eq.{execution_id}",
                              "scope": f"eq.{self.scope}"},
                      body={"status": status, "finished_at": to_iso(utcnow()),
                            "summary": summary, "error": error},
                      prefer="return=minimal")

    def recent_executions(self, limit=20):
        return self._select("executions", {}, limit=limit,
                            order="started_at.desc")

    # --- locks ---------------------------------------------------------------
    def acquire_lock(self, name, ttl_seconds, holder):
        now = utcnow()
        expires = to_iso(now + timedelta(seconds=ttl_seconds))
        try:
            self._request("POST", "locks",
                          body={"scope": self.scope, "name": name,
                                "holder": holder, "expires_at": expires},
                          prefer="return=minimal")
            return True
        except ConflictError:
            pass
        # Somebody holds it. We may only take it over once their lease has run
        # out — or if it is ours already (a re-entrant tick).
        updated = self._request(
            "PATCH", "locks",
            params={"scope": f"eq.{self.scope}", "name": f"eq.{name}",
                    "or": f"(expires_at.lt.{to_iso(now)},holder.eq.{holder})"},
            body={"holder": holder, "expires_at": expires},
            prefer="return=representation")
        return bool(updated)

    def release_lock(self, name, holder):
        try:
            self._request("DELETE", "locks",
                          params={"scope": f"eq.{self.scope}",
                                  "name": f"eq.{name}",
                                  "holder": f"eq.{holder}"})
        except StorageError:
            pass   # the lease expires on its own anyway

    # --- settings ------------------------------------------------------------
    def get_settings(self):
        rows = self._select("settings", {})
        return {r["key"]: r.get("value") for r in rows}

    def set_setting(self, key, value):
        self._upsert("settings",
                     {"scope": self.scope, "key": key, "value": value,
                      "updated_at": to_iso(utcnow())},
                     on_conflict="scope,key")

    # --- decisions -----------------------------------------------------------
    def save_decision(self, source, model, decision, input_digest=None):
        rows = self._request("POST", "agent_decisions", body={
            "scope": self.scope, "source": source, "model": model,
            "decision": decision, "input_digest": input_digest,
        }, prefer="return=representation")
        return (rows or [{}])[0]

    def recent_decisions(self, limit=10):
        return self._select("agent_decisions", {}, limit=limit,
                            order="created_at.desc")

    # --- health --------------------------------------------------------------
    def ping(self):
        try:
            self._select("settings", {}, limit=1)
            return True
        except StorageError:
            return False


class ConflictError(StorageError):
    """A UNIQUE constraint said no. Usually the correct answer, not an error."""

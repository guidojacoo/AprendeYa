"""Local backend: the JSON files fantasybot has always written.

This is not a compatibility shim bolted on afterwards — it is the original
storage, lifted behind the interface unchanged. The file layout is identical
(`.state/snapshot.json`, `.state/events.jsonl`, `tokens.json`, `.cache/`), so an
existing checkout keeps working with no migration, and `scripts/migrate-state.py`
can read it back out later.

The pieces the original never had (scheduled actions, executions, locks) are
stored the same way: one small JSON file each, written atomically.
"""

import json
import os
import time
import uuid
from datetime import timedelta

from .. import config
from .base import (CANCELLED, PENDING, RUNNING, Storage, parse_iso,
                   to_iso, utcnow)

STATE_DIR = os.path.join(config.ROOT, ".state")
CACHE_DIR = os.path.join(config.ROOT, ".cache")
EVENTS_PATH = os.path.join(STATE_DIR, "events.jsonl")
MAX_EVENTS = 5000

# Document name -> where it lives. Anything not listed falls back to
# `.state/<name>.json`, which is how value-history days and future documents land
# without needing an entry here.
_SPECIAL_PATHS = {
    "tokens": os.path.join(config.ROOT, "tokens.json"),
    "pkce": os.path.join(config.ROOT, ".pkce.json"),
    "run_current": os.path.join(STATE_DIR, "run.current"),
}


def _doc_path(name):
    if name in _SPECIAL_PATHS:
        return _SPECIAL_PATHS[name]
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return os.path.join(STATE_DIR, f"{safe}.json")


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, value):
    """Atomic write: a tick killed mid-flight leaves the old file, not half a new
    one. Same behaviour the original `state._write` relied on."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


class LocalStorage(Storage):
    kind = "local"

    # --- documents -----------------------------------------------------------
    def get_doc(self, name, default=None):
        return _read(_doc_path(name), default)

    def put_doc(self, name, value):
        _write(_doc_path(name), value)

    def delete_doc(self, name):
        try:
            os.remove(_doc_path(name))
        except OSError:
            pass

    # --- TTL cache -----------------------------------------------------------
    def _cache_path(self, key):
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
        return os.path.join(CACHE_DIR, safe + ".json")

    def cache_get(self, key):
        path = self._cache_path(key)
        if not os.path.exists(path):
            return None
        # The TTL lives in the file's mtime, exactly as the original cache did,
        # so a pre-existing .cache/ directory stays valid across this upgrade.
        try:
            age = time.time() - os.path.getmtime(path)
        except OSError:
            return None
        entry = _read(path, None)
        if entry is None:
            return None
        ttl = entry.get("__ttl") if isinstance(entry, dict) else None
        if ttl is not None and age >= ttl:
            return None
        if isinstance(entry, dict) and "__ttl" in entry:
            return entry.get("value")
        return entry   # legacy file written before TTLs were stored inline

    def cache_put(self, key, value, ttl_seconds):
        _write(self._cache_path(key), {"__ttl": ttl_seconds, "value": value})

    def cache_clear(self):
        if os.path.isdir(CACHE_DIR):
            for name in os.listdir(CACHE_DIR):
                try:
                    os.remove(os.path.join(CACHE_DIR, name))
                except OSError:
                    pass

    # --- events --------------------------------------------------------------
    def emit_event(self, event):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(EVENTS_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._trim_events()
        except OSError:
            pass   # telemetry must never break an action
        return event

    def _trim_events(self):
        try:
            with open(EVENTS_PATH, encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > MAX_EVENTS:
                with open(EVENTS_PATH, "w", encoding="utf-8") as f:
                    f.writelines(lines[-MAX_EVENTS:])
        except OSError:
            pass

    def load_events(self, limit=500):
        try:
            with open(EVENTS_PATH, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
        out = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    # --- market snapshots ----------------------------------------------------
    def get_market_snapshot(self, day_iso):
        value = self.get_doc(f"value_history_{day_iso}", {})
        return value if isinstance(value, dict) else {}

    def put_market_snapshot(self, day_iso, values, keep_days=40):
        self.put_doc(f"value_history_{day_iso}", values)
        self._prune_snapshots(day_iso, keep_days)

    def _prune_snapshots(self, day_iso, keep_days):
        from datetime import date
        try:
            cutoff = date.fromisoformat(day_iso) - timedelta(days=keep_days)
        except ValueError:
            return
        try:
            names = os.listdir(STATE_DIR)
        except OSError:
            return
        for name in names:
            if not (name.startswith("value_history_") and name.endswith(".json")):
                continue
            stem = name[len("value_history_"):-len(".json")]
            try:
                day = date.fromisoformat(stem)
            except ValueError:
                continue
            if day < cutoff:
                try:
                    os.remove(os.path.join(STATE_DIR, name))
                except OSError:
                    pass

    # --- scheduled actions ---------------------------------------------------
    def _actions(self):
        rows = self.get_doc("scheduled_actions", [])
        return rows if isinstance(rows, list) else []

    def schedule_action(self, action_type, payload, execute_at, idempotency_key,
                        expires_at=None):
        rows = self._actions()
        for row in rows:
            if row.get("idempotency_key") == idempotency_key:
                # Already known. Only a still-pending action may be re-timed; a
                # finished one stays finished (that is the anti-double-bid rule).
                if row.get("status") == PENDING:
                    row["execute_at"] = to_iso(execute_at)
                    row["payload"] = payload
                    row["expires_at"] = to_iso(expires_at)
                    self.put_doc("scheduled_actions", rows)
                return row
        row = {
            "id": uuid.uuid4().hex,
            "type": action_type,
            "payload": payload,
            "execute_at": to_iso(execute_at),
            "expires_at": to_iso(expires_at),
            "idempotency_key": idempotency_key,
            "status": PENDING,
            "attempts": 0,
            "locked_until": None,
            "result": None,
            "last_error": None,
            "created_at": to_iso(utcnow()),
        }
        rows.append(row)
        self.put_doc("scheduled_actions", rows)
        return row

    def _reclaimable(self, row, now):
        """A `running` row whose lease expired is up for grabs again: the tick
        holding it died (Vercel timeout, runner killed) and never finished."""
        if row.get("status") != RUNNING:
            return False
        until = parse_iso(row.get("locked_until"))
        return until is None or until <= now

    def due_actions(self, now=None, limit=25):
        now = now or utcnow()
        out = []
        for row in self._actions():
            if row.get("status") != PENDING and not self._reclaimable(row, now):
                continue
            at = parse_iso(row.get("execute_at"))
            if at is None or at > now:
                continue
            out.append(row)
        out.sort(key=lambda r: r.get("execute_at") or "")
        return out[:limit]

    def pending_actions(self, limit=50):
        rows = [r for r in self._actions()
                if r.get("status") in (PENDING, RUNNING)]
        rows.sort(key=lambda r: r.get("execute_at") or "")
        return rows[:limit]

    def claim_action(self, action, lease_seconds=120):
        now = utcnow()
        rows = self._actions()
        for row in rows:
            if row.get("id") != action.get("id"):
                continue
            if row.get("status") != PENDING and not self._reclaimable(row, now):
                return False
            row["status"] = RUNNING
            row["attempts"] = (row.get("attempts") or 0) + 1
            row["locked_until"] = to_iso(now + timedelta(seconds=lease_seconds))
            self.put_doc("scheduled_actions", rows)
            action.update(row)
            return True
        return False

    def finish_action(self, action, status, result=None, error=None):
        rows = self._actions()
        for row in rows:
            if row.get("id") == action.get("id"):
                row["status"] = status
                row["result"] = result
                row["last_error"] = error
                row["locked_until"] = None
                row["finished_at"] = to_iso(utcnow())
        self.put_doc("scheduled_actions", rows)

    def cancel_action(self, idempotency_key):
        rows = self._actions()
        changed = False
        for row in rows:
            if row.get("idempotency_key") == idempotency_key and row.get("status") == PENDING:
                row["status"] = CANCELLED
                changed = True
        if changed:
            self.put_doc("scheduled_actions", rows)
        return changed

    def next_deadline(self):
        times = [parse_iso(r.get("execute_at")) for r in self._actions()
                 if r.get("status") == PENDING]
        times = [t for t in times if t]
        return min(times) if times else None

    # --- executions ----------------------------------------------------------
    def start_execution(self, trigger):
        rows = self.get_doc("executions", [])
        if not isinstance(rows, list):
            rows = []
        row = {"id": uuid.uuid4().hex, "trigger": trigger,
               "started_at": to_iso(utcnow()), "status": RUNNING,
               "finished_at": None, "summary": None, "error": None}
        rows.append(row)
        self.put_doc("executions", rows[-200:])
        return row["id"]

    def finish_execution(self, execution_id, status, summary=None, error=None):
        rows = self.get_doc("executions", [])
        if not isinstance(rows, list):
            return
        for row in rows:
            if row.get("id") == execution_id:
                row.update({"status": status, "finished_at": to_iso(utcnow()),
                            "summary": summary, "error": error})
        self.put_doc("executions", rows)

    def recent_executions(self, limit=20):
        rows = self.get_doc("executions", [])
        if not isinstance(rows, list):
            return []
        return list(reversed(rows[-limit:]))

    # --- locks ---------------------------------------------------------------
    def acquire_lock(self, name, ttl_seconds, holder):
        now = utcnow()
        locks = self.get_doc("locks", {})
        if not isinstance(locks, dict):
            locks = {}
        cur = locks.get(name)
        if cur:
            expires = parse_iso(cur.get("expires_at"))
            if expires and expires > now and cur.get("holder") != holder:
                return False
        locks[name] = {"holder": holder,
                       "expires_at": to_iso(now + timedelta(seconds=ttl_seconds))}
        self.put_doc("locks", locks)
        return True

    def release_lock(self, name, holder):
        locks = self.get_doc("locks", {})
        if isinstance(locks, dict) and locks.get(name, {}).get("holder") == holder:
            locks.pop(name, None)
            self.put_doc("locks", locks)

    # --- settings ------------------------------------------------------------
    def get_settings(self):
        s = self.get_doc("settings", {})
        return s if isinstance(s, dict) else {}

    def set_setting(self, key, value):
        s = self.get_settings()
        s[key] = value
        self.put_doc("settings", s)

    # --- decisions -----------------------------------------------------------
    def save_decision(self, source, model, decision, input_digest=None):
        rows = self.get_doc("decisions", [])
        if not isinstance(rows, list):
            rows = []
        row = {"id": uuid.uuid4().hex, "created_at": to_iso(utcnow()),
               "source": source, "model": model, "decision": decision,
               "input_digest": input_digest}
        rows.append(row)
        self.put_doc("decisions", rows[-100:])
        return row

    def recent_decisions(self, limit=10):
        rows = self.get_doc("decisions", [])
        if not isinstance(rows, list):
            return []
        return list(reversed(rows[-limit:]))

    # --- health --------------------------------------------------------------
    def ping(self):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            return True
        except OSError:
            return False

"""Backend selection.

One process, one backend, decided once from the environment:

    FANTASYBOT_STORAGE=local      JSON files under .state/ (the CLI default)
    FANTASYBOT_STORAGE=supabase   PostgreSQL (what Vercel functions use)

With nothing set it auto-detects: Supabase credentials present means Supabase,
otherwise local. So `python -m fantasybot team` on a laptop keeps behaving like
it always did, and the same code on Vercel picks up the database with no flag.
"""

from .. import config
from .base import (CANCELLED, DONE, FAILED, PENDING, RUNNING, SKIPPED,
                   Storage, StorageError, StorageUnavailable, parse_iso, to_iso, utcnow)

_backend = None


def get_storage():
    """The process-wide backend (built once, reused)."""
    global _backend
    if _backend is None:
        _backend = _build()
    return _backend


def _build():
    kind = (config.STORAGE_BACKEND or "local").strip().lower()
    if kind == "supabase":
        from .supabase import SupabaseStorage
        return SupabaseStorage()
    if kind == "local":
        from .local import LocalStorage
        return LocalStorage()
    raise StorageError(f"Unknown FANTASYBOT_STORAGE={kind!r} (use 'local' or 'supabase')")


def set_storage(backend):
    """Swap the backend. Tests use it; so does `scripts/migrate-state.py`, which
    needs to hold both backends open at once to copy between them."""
    global _backend
    _backend = backend
    return backend


def reset_storage():
    global _backend
    _backend = None


__all__ = ["get_storage", "set_storage", "reset_storage", "Storage",
           "StorageError", "StorageUnavailable", "utcnow", "to_iso", "parse_iso",
           "PENDING", "RUNNING", "DONE", "FAILED", "CANCELLED", "SKIPPED"]

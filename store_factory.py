"""
Shared NoshyStore singleton.

All Noshy modules (decorator, context, hooks, server) should obtain their
store via `get_store()` from this module instead of constructing their own.
This avoids opening N redundant SQLite connections + N embedder instances
against the same database file.
"""
import threading
from typing import Optional

_store = None
_lock = threading.Lock()


def get_store(db_path: str = None, embedder=None):
    """Return the process-wide NoshyStore singleton.

    First call creates it; subsequent calls ignore the args. Pass
    explicit db_path/embedder only on first access (or after reset_store()).
    """
    global _store
    if _store is not None:
        return _store
    with _lock:
        if _store is None:
            from store import NoshyStore
            if embedder is None:
                from embed import auto_embedder
                embedder = auto_embedder()
            _store = NoshyStore(db_path=db_path, embedder=embedder)
    return _store


def reset_store(store=None):
    """Replace the singleton — primarily for tests.

    Pass an explicit NoshyStore to install it, or None to clear so the
    next get_store() builds a fresh one.
    """
    global _store
    with _lock:
        if _store is not None and store is not _store:
            try:
                _store.shutdown()
            except Exception:
                pass
        _store = store

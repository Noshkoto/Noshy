"""
Noshy Python decorators — wrap any function so its outputs (and errors) are
automatically stored as memories. Plus a context-manager session helper.

Example:

    import noshy

    @noshy.remember(topic="deploy", importance="high")
    def deploy(env):
        return f"deployed to {env}"

    deploy("prod")              # stores a memory
    noshy.recall("deploy")      # finds it

    with noshy.session(project="onboarding"):
        ...                     # everything stored picks up the project
"""
import os
import sys
import json
import time
import inspect
import logging
import functools
import threading
import traceback
from typing import Optional, List, Dict, Any, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from store import NoshyStore
from embed import auto_embedder

log = logging.getLogger("noshy.decorator")

_store: Optional[NoshyStore] = None
_store_lock = threading.Lock()

# Thread-local stack of active sessions (project + tags pushed by `session()`)
_ctx = threading.local()


def get_store() -> NoshyStore:
    """Lazy, thread-safe singleton."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = NoshyStore(embedder=auto_embedder())
    return _store


def reset_store(store: NoshyStore = None):
    """Override the global store — primarily for tests."""
    global _store
    _store = store


def _stack() -> List[Dict[str, Any]]:
    s = getattr(_ctx, "stack", None)
    if s is None:
        s = []
        _ctx.stack = s
    return s


def _current_project(default: str = "default") -> str:
    for frame in reversed(_stack()):
        if frame.get("project"):
            return frame["project"]
    return default


def _current_tags() -> List[str]:
    tags: List[str] = []
    for frame in _stack():
        tags.extend(frame.get("tags", []) or [])
    return tags


class session:
    """Context manager + decorator: scope memories to a project and tags.

    Usage:
        with noshy.session(project="checkout-bugfix", tags=["sprint-23"]):
            do_work()                # any @remember calls inherit project/tags

        @noshy.session(project="background-job")
        def job(): ...
    """

    def __init__(self, project: str = None, tags: List[str] = None):
        self.project = project
        self.tags = list(tags or [])

    def __enter__(self):
        _stack().append({"project": self.project, "tags": self.tags})
        return self

    def __exit__(self, exc_type, exc, tb):
        stack = _stack()
        if stack:
            stack.pop()
        return False

    def __call__(self, fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with self:
                return fn(*args, **kwargs)
        return wrapper


def remember(
    topic: str = None,
    *,
    importance: str = "medium",
    project: str = None,
    keywords: List[str] = None,
    on_error: bool = True,
    ttl_seconds: int = None,
    summarize: Callable[[Any, tuple, dict], str] = None,
    capture_args: bool = False,
    skip_if: Callable[[Any], bool] = None,
):
    """Decorator: store a memory each time the wrapped function returns.

    Args:
        topic: Topic slug. Defaults to "fn-<module>.<name>".
        importance: Default importance (critical/high/medium/low).
        project: Project scope; falls back to the active noshy.session().
        keywords: Extra keywords merged with the function name and tags.
        on_error: If True, exceptions are recorded as high-importance memories.
        ttl_seconds: Auto-expire after this many seconds.
        summarize: Custom (result, args, kwargs) -> str summary builder.
        capture_args: If True, include args/kwargs in the summary (default off
                      so you don't accidentally store secrets).
        skip_if: Optional (result) -> bool to skip storage for a given return.

    The wrapped function's behaviour is unchanged; memory writes are best-effort
    and never raise into the caller.
    """
    def decorate(fn):
        slug = topic or f"fn-{fn.__module__}.{fn.__qualname__}"
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            sig = None

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                if on_error:
                    try:
                        tb = traceback.format_exc(limit=3)
                        excerpt = tb.strip().splitlines()[-1][:200]
                        _safe_store(
                            topic=slug + "-error",
                            summary=f"{fn.__qualname__} raised {type(e).__name__}: {e}",
                            raw_excerpt=excerpt,
                            keywords=(keywords or []) + ["error", type(e).__name__],
                            importance="high",
                            project=project,
                            ttl_seconds=ttl_seconds,
                        )
                    except Exception:
                        pass
                raise

            dt = (time.perf_counter() - t0) * 1000
            try:
                if skip_if and skip_if(result):
                    return result
                if summarize:
                    summary = summarize(result, args, kwargs)
                elif capture_args:
                    summary = (
                        f"{fn.__qualname__}({_format_call(sig, args, kwargs)}) -> "
                        f"{_safe_repr(result)} [{dt:.0f}ms]"
                    )
                else:
                    summary = f"{fn.__qualname__} -> {_safe_repr(result)} [{dt:.0f}ms]"
                if summary and len(summary.strip()) >= 10:
                    _safe_store(
                        topic=slug,
                        summary=summary,
                        keywords=(keywords or []) + [fn.__qualname__],
                        importance=importance,
                        project=project,
                        ttl_seconds=ttl_seconds,
                    )
            except Exception as e:
                log.debug(f"remember() store failed: {e}")
            return result

        return wrapper

    # Support both @remember and @remember(...)
    if callable(topic):
        fn = topic
        topic = None
        return decorate(fn)
    return decorate


def recall(query: str, *, limit: int = 10, project: str = None,
           mode: str = "hybrid") -> List[Dict]:
    """Convenience recall helper that honors the active session."""
    p = project or _current_project()
    s = get_store()
    if mode == "keyword":
        return s.recall_by_topic(query, limit=limit, project=p)
    return s.recall_hybrid(query, limit=limit, project=p)


# ──────────── helpers ────────────

def _safe_store(*, topic: str, summary: str, raw_excerpt: str = None,
                keywords: List[str] = None, importance: str = "medium",
                project: str = None, ttl_seconds: int = None) -> Optional[str]:
    """Best-effort store. Returns id on success, None on failure."""
    s = get_store()
    proj = project or _current_project()
    kw = list(keywords or []) + _current_tags()
    try:
        return s.store_memory(
            topic=topic,
            summary=summary,
            raw_excerpt=raw_excerpt,
            keywords=kw or None,
            importance=importance,
            project=proj,
            source="decorator",
            ttl_seconds=ttl_seconds,
        )
    except Exception as e:
        log.debug(f"store_memory failed: {e}")
        return None


def _format_call(sig, args, kwargs, maxlen: int = 240) -> str:
    """Render a function call signature with secrets redacted by parameter name."""
    parts: List[str] = []
    if sig is None:
        # Best-effort fallback: drop kwargs that look secret, leave positional
        parts.append(_safe_repr(list(args), maxlen=maxlen))
        kw_red = {k: ("***" if _looks_secret(k) else v) for k, v in kwargs.items()}
        if kw_red:
            parts.append(_safe_repr(kw_red, maxlen=maxlen))
        return ", ".join(p for p in parts if p)
    try:
        bound = sig.bind_partial(*args, **kwargs)
        bound.apply_defaults()
    except TypeError:
        return _safe_repr(list(args), kwargs, maxlen=maxlen)
    for name, value in bound.arguments.items():
        if _looks_secret(name):
            parts.append(f"{name}=***")
        else:
            parts.append(f"{name}={_safe_repr(value, maxlen=maxlen)}")
    return ", ".join(parts)


def _safe_repr(*objs, maxlen: int = 240) -> str:
    """Repr that drops large/sensitive payloads down to a manageable size."""
    out_parts = []
    for o in objs:
        try:
            if isinstance(o, (bytes, bytearray)):
                out_parts.append(f"<{len(o)} bytes>")
            elif isinstance(o, dict):
                redacted = {
                    k: ("***" if _looks_secret(k) else v) for k, v in o.items()
                }
                out_parts.append(json.dumps(redacted, default=str)[:maxlen])
            elif isinstance(o, (list, tuple, set)):
                out_parts.append(json.dumps(list(o), default=str)[:maxlen])
            else:
                s = repr(o)
                out_parts.append(s if len(s) <= maxlen else s[:maxlen] + "…")
        except Exception:
            out_parts.append("<unrepr>")
    return ", ".join(out_parts)


_SECRET_HINTS = ("password", "secret", "token", "api_key", "apikey",
                 "authorization", "auth", "cookie", "session")


def _looks_secret(key: str) -> bool:
    k = (key or "").lower()
    return any(h in k for h in _SECRET_HINTS)

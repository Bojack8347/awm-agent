"""Small, safe tracing adapter used by business services.

Business flows should be observable, but tracing must never become part of the
critical path. This helper accepts either a dependency object with
``db_create_trace_event`` or falls back to the persistence function directly.
All exceptions are swallowed after a short log line.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional


def safe_trace_event(deps: Optional[Any] = None, **kwargs: Any) -> Optional[Any]:
    """Append a trace event when tracing is available.

    The function deliberately returns ``None`` on every failure so callers can
    add tracing to hot business paths without changing user-visible behavior.
    """
    create_fn: Optional[Callable[..., Any]] = None
    if deps is not None:
        create_fn = getattr(deps, "db_create_trace_event", None)
    if create_fn is None:
        try:
            from api.persistence import create_trace_event  # type: ignore

            create_fn = create_trace_event
        except Exception:
            create_fn = None
    if create_fn is None:
        return None


def safe_trace_events(events: Iterable[dict[str, Any]]) -> int:
    """Append multiple trace rows best-effort in one database transaction."""
    try:
        from api.persistence import create_trace_events  # type: ignore

        return int(create_trace_events(events) or 0)
    except Exception as exc:  # pragma: no cover - tracing is best-effort
        print(f"[trace] safe_trace_events skipped: {exc}", flush=True)
        return 0

    try:
        return create_fn(**kwargs)
    except Exception as exc:  # pragma: no cover - tracing is best-effort
        print(f"[trace] safe_trace_event skipped: {exc}", flush=True)
        return None


__all__ = ["safe_trace_event", "safe_trace_events"]

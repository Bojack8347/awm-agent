"""Safe business-event publisher.

Use this for durable product triggers. The outbox can later be drained by a
worker, Cloud Task, or event bridge without changing the producer call sites.
"""

from __future__ import annotations

from typing import Any, Callable, Optional


def safe_publish_event(deps: Optional[Any] = None, **kwargs: Any) -> Optional[Any]:
    """Publish a business event when the outbox is available."""
    create_fn: Optional[Callable[..., Any]] = None
    if deps is not None:
        create_fn = getattr(deps, "db_create_business_event", None)
    if create_fn is None:
        try:
            from api.persistence import create_business_event  # type: ignore

            create_fn = create_business_event
        except Exception:
            create_fn = None
    if create_fn is None:
        return None

    try:
        return create_fn(**kwargs)
    except Exception as exc:  # pragma: no cover - event publishing is best-effort
        print(f"[events] safe_publish_event skipped: {exc}", flush=True)
        return None


__all__ = ["safe_publish_event"]

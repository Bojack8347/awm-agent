"""Background orchestration helpers for API request handlers."""

from __future__ import annotations

from typing import Any, Dict


def pipeline_checkpoint(
    deps: Any,
    run_id: str,
    pipeline_name: str,
    step_name: str,
    context: Dict[str, Any],
) -> None:
    """Persist a pipeline checkpoint with non-serializable values normalized."""
    ctx_copy = dict(context)
    truth = ctx_copy.get("truth_result")
    if truth and hasattr(truth, "__dict__"):
        ctx_copy["truth_result"] = {
            "committed": getattr(truth, "committed", []),
            "snapshot_version": getattr(truth, "snapshot_version", 0),
            "pending_confirmations": getattr(truth, "pending_confirmations", []),
        }
    deps.db_store_checkpoint(run_id, pipeline_name, step_name, ctx_copy)


__all__ = [
    "pipeline_checkpoint",
]


# ---------------------------------------------------------------------------
# Server-facing wrappers. Blueprints call these through the api.server facade
# so tests can still monkeypatch server-level dependencies in one place.
# ---------------------------------------------------------------------------

def _server_deps() -> Any:
    import sys

    package_name = __package__
    return sys.modules.get(package_name) or sys.modules.get("server") or sys.modules[__name__]


def _app_deps() -> Any:
    return _server_deps()


def _pipeline_checkpoint(
    run_id: str, pipeline_name: str, step_name: str, context: Dict[str, Any],
) -> None:
    pipeline_checkpoint(_server_deps(), run_id, pipeline_name, step_name, context)

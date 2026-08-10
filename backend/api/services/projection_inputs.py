"""Shared cash-flow projection input assembly."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, Tuple


def projection_cashflow_input(
    latest_knowledge_snapshot: Callable[[str], Dict[str, Any] | None],
    client_id: str,
) -> Tuple[Dict[str, Any], Any]:
    """Return the persisted cashflow state and snapshot version for Projection."""

    try:
        snapshot = latest_knowledge_snapshot(client_id) or {}
    except Exception:  # pylint: disable=broad-except
        return {}, None
    snapshot_data = (
        snapshot.get("snapshot_data")
        if isinstance(snapshot.get("snapshot_data"), dict)
        else {}
    )
    cashflow_state = snapshot_data.get("cashflow_state")
    payload = dict(cashflow_state) if isinstance(cashflow_state, dict) else {}

    try:
        from advisor.tools.deterministic_tools.execution import (
            build_cashflow_payload_from_client_file,
        )
        from api.services.client_state_view import build_client_state_view

        state_view = build_client_state_view(client_id)
        if projection_state_has_source_inputs(state_view):
            latest_payload = build_cashflow_payload_from_client_file(state_view)
            payload = merge_projection_payload(payload, latest_payload)
    except Exception:  # pylint: disable=broad-except
        pass

    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return payload, f"{snapshot.get('version')}:{fingerprint}"


def merge_projection_payload(
    base: Dict[str, Any],
    latest: Dict[str, Any],
) -> Dict[str, Any]:
    """Merge newer confirmed fields without erasing richer snapshot branches."""

    merged = dict(base)
    for key, value in latest.items():
        if value in (None, {}, []):
            continue
        prior = merged.get(key)
        if isinstance(value, dict) and isinstance(prior, dict):
            merged[key] = merge_projection_payload(prior, value)
        else:
            merged[key] = value
    return merged


def projection_state_has_source_inputs(state_view: Any) -> bool:
    if not isinstance(state_view, dict):
        return False
    for key in ("cashflow_state", "facts", "structured_facts"):
        value = state_view.get(key)
        if isinstance(value, dict) and value:
            return True
    return False


def projection_input_fingerprint(snapshot_version: Any) -> str:
    text = str(snapshot_version or "")
    return text.rsplit(":", 1)[-1] if ":" in text else text


__all__ = [
    "merge_projection_payload",
    "projection_cashflow_input",
    "projection_input_fingerprint",
    "projection_state_has_source_inputs",
]

"""Shared deterministic-tool cache helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def compute_tool_cache_key(
    tool_name: str,
    payload: Any,
    *,
    namespace: str = "",
) -> str:
    """Compute a stable hash key for memoizing tool call results."""
    fingerprint_input = (
        {"namespace": namespace, "payload": payload}
        if namespace
        else payload
    )
    canonical = json.dumps(
        fingerprint_input,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return f"{tool_name}:{digest}"

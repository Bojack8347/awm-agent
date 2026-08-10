"""Journey runtime HTTP helpers."""

from __future__ import annotations

import os
from typing import Any

from flask import jsonify


def _journey_runtime_v2_enabled() -> bool:
    """JOURNEY_RUNTIME_V2 gates the new endpoints. Default off during cutover."""
    return os.getenv("JOURNEY_RUNTIME_V2", "false").strip().lower() in {"1", "true", "yes", "on"}


def _v2_disabled_response() -> Any:
    return jsonify({
        "success": False,
        "error": "JOURNEY_RUNTIME_V2 disabled — endpoint not available in this deploy",
    }), 503

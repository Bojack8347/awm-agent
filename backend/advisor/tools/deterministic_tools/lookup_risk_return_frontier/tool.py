"""Agent tool declaration for lookupRiskReturnFrontier."""

from __future__ import annotations


TOOL_SPEC = {
    "name": "lookupRiskReturnFrontier",
    "capability": "risk_return_frontier",
    "description": (
        "Look up the deterministic portfolio frontier for an explicitly required annual return "
        "or target annual volatility. Use to test feasibility or identify the paired frontier "
        "risk/return value; do not use it to construct holdings, select assumptions, or make a recommendation."
    ),
    "writeback_target": "none",
    "read_only": True,
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "required_return_pct": {
            "type": "number",
            "description": "Explicit required annual return in percentage points, for example 7.5 for 7.5%; omit when querying by volatility.",
        },
        "target_volatility_pct": {
            "type": "number",
            "description": "Explicit target annual volatility in percentage points, for example 10 for 10%; omit when querying by required return.",
        },
    },
    "additionalProperties": False,
}

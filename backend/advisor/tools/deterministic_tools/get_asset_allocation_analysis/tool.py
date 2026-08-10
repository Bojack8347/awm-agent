"""Agent tool declaration for retrieving a completed allocation analysis."""

from __future__ import annotations


TOOL_SPEC = {
    "name": "get_asset_allocation_analysis",
    "capability": "allocation_retrieval",
    "description": (
        "Retrieve a completed validated asset-allocation analysis for the current client "
        "without rerunning the optimizer. Use for follow-up questions about prior return, "
        "volatility, target tolerance, holdings, sleeves, dollars, reconciliation, exclusions, "
        "or limitations. Pass null for the latest analysis in this conversation, or an "
        "analysis_id returned by run_asset_allocation. This is only for a later follow-up: never "
        "call it after run_asset_allocation already returned the same analysis in the current "
        "specialist call. Do not use after changing a signed mandate."
    ),
    "writeback_target": "none",
    "read_only": True,
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis_id": {
            "anyOf": [
                {"type": "string", "minLength": 1, "maxLength": 160},
                {"type": "null"},
            ],
            "description": (
                "A prior allocation analysis_id, or null for the latest in this conversation."
            ),
        },
    },
    "required": ["analysis_id"],
    "additionalProperties": False,
}

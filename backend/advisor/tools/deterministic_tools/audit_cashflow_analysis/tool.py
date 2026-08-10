"""Agent declaration for auditing one immutable cash-flow analysis."""

TOOL_SPEC = {
    "name": "audit_cashflow_analysis",
    "capability": "calculation_toolkit",
    "description": (
        "Programmatically audit a completed cash-flow analysis without rerunning "
        "LifeModel. The tool reads the immutable stored snapshot, verifies terminal "
        "metric reconciliation, event-distribution probability mass, path-count "
        "metadata, deterministic first-event timing when annual series are available, "
        "and contradictions such as a negative terminal value paired with 'never "
        "depleted'. Use when the client asks to show, verify, reconcile, challenge, "
        "or audit the calculation. Pass null for the latest analysis in this "
        "conversation. This tool reports consistency only; it does not produce a "
        "financial recommendation or replace the source model."
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
                "A prior cash-flow analysis_id, or null for the latest analysis "
                "in the current conversation."
            ),
        },
    },
    "required": ["analysis_id"],
    "additionalProperties": False,
}

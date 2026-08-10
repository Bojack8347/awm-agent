"""Agent tool declaration for record_assessment_signoff."""

from __future__ import annotations


TOOL_SPEC = {
    "name": "record_assessment_signoff",
    "capability": "assessment_signoff",
    "description": (
        "Record the client's explicit decision on a versioned Investment-Assessment for a "
        "specific money pool. Use signed_off=true only after positive sign-off; use "
        "signed_off=false only when the client explicitly declines or cancels sign-off. "
        "Only positive sign-off unlocks proposal building for that assessment."
    ),
    "writeback_target": "client_file.plans",
    "read_only": False,
    "irreversible": True,
    "requires_explicit_consent": True,
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "investment_consultation_id": {"type": "string"},
        "assessment_id": {"type": "string"},
        "assessment_version": {"type": "integer", "minimum": 1},
        "money_pool_id": {"type": "string"},
        "pool_label": {"type": "string"},
        "signed_off": {"type": "boolean"},
        "decision_source": {"type": "string"},
    },
    "required": [
        "assessment_id",
        "assessment_version",
        "money_pool_id",
        "signed_off",
    ],
    "additionalProperties": False,
}

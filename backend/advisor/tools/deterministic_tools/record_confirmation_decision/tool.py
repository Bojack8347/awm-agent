"""Agent tool declaration for confirmation-decision auditing."""

TOOL_SPEC = {
    "name": "record_confirmation_decision",
    "capability": "client_file_facts",
    "read_only": False,
    "description": (
        "Audit the semantic decision for pending Client File facts. Call once whenever pending "
        "facts are confirmed, rejected, corrected, or remain ambiguous. This audit never authorizes "
        "or blocks a fact write."
    ),
    "writeback_target": "client_file.confirmation_decisions",
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "draft_id": {"type": "string"},
        "field": {"type": "string"},
        "proposed_value": {},
        "decision": {
            "type": "string",
            "enum": ["confirmed", "rejected", "corrected", "ambiguous"],
        },
        "rationale": {"type": "string"},
        "database_action": {
            "type": "object",
            "properties": {
                "operation": {"type": "string"},
                "ok": {"type": "boolean"},
                "result_id": {"type": "string"},
            },
            "required": ["operation", "ok"],
            "additionalProperties": False,
        },
    },
    "required": [
        "draft_id",
        "field",
        "proposed_value",
        "decision",
        "rationale",
        "database_action",
    ],
    "additionalProperties": False,
}

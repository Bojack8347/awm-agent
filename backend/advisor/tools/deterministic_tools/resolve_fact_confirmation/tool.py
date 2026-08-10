TOOL_SPEC = {
    "name": "resolve_fact_confirmation",
    "capability": "client_file_facts",
    "read_only": False,
    "description": "Resolve only the exact fields in one authenticated prompt-bound confirmation set.",
    "writeback_target": "client_file.facts",
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "confirmation_set_id": {"type": "string", "minLength": 1},
        "prompt_message_id": {"type": "string", "minLength": 1},
        "client_message": {"type": "string"},
        "decisions": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "confirmation_item_id": {"type": "string", "minLength": 1},
                    "decision": {"type": "string", "enum": ["confirmed", "corrected", "rejected", "ambiguous", "deferred"]},
                    "corrected_value": {},
                },
                "required": ["confirmation_item_id", "decision"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["confirmation_set_id", "prompt_message_id", "decisions"],
    "additionalProperties": False,
}

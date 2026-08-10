TOOL_SPEC = {
    "name": "present_fact_confirmation",
    "capability": "client_file_facts",
    "read_only": False,
    "description": "Create an immutable prompt-bound confirmation set for exact pending draft fields.",
    "writeback_target": "client_file.fact_confirmation_sets",
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "source_turn_id": {"type": "string", "minLength": 1},
        "items": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string", "minLength": 1},
                    "field": {
                        "type": "string",
                        "minLength": 1,
                        "description": "A canonical fact field or a pending entity_id.",
                    },
                    "atomic_group_id": {"type": "string"},
                    "resolution_mode": {"type": "string", "enum": ["independent", "all_or_none"]},
                },
                "required": ["draft_id", "field"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

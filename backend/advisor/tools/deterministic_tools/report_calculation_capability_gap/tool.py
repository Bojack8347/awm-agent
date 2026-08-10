"""Agent declaration for the terminal calculation capability buffer."""

TOOL_SPEC = {
    "name": "report_calculation_capability_gap",
    "capability": "calculation_toolkit",
    "description": (
        "Stop a calculation or exact scenario request when no registered calculation "
        "tool can represent the entire requested operation in one validated call. Use this "
        "instead of approximating, calculating in prose, copying a derived result into "
        "another calculator, or retrying a nearby but semantically incorrect tool. Do "
        "not use it merely because an otherwise supported operation is missing one "
        "client-supplied input; let that calculator return its typed missing-input error."
    ),
    "writeback_target": "none",
    "read_only": True,
}


PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "reason_code": {
            "type": "string",
            "enum": [
                "unsupported_operation",
                "cross_domain_calculation",
                "unsupported_source",
                "external_solver_unavailable",
                "external_solver_unvalidated",
            ],
            "description": (
                "The narrow capability boundary that prevents one validated calculator "
                "call from answering the complete follow-up."
            ),
        },
        "request_summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240,
            "description": (
                "A short internal summary of the requested calculation. It is retained "
                "for diagnostics and is not copied into the client-facing response."
            ),
        },
        "source_refs": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 180},
            "maxItems": 8,
            "description": (
                "Known immutable analysis or evidence identifiers relevant to the "
                "request. Use an empty array when none are available."
            ),
        },
    },
    "required": ["reason_code", "request_summary", "source_refs"],
    "additionalProperties": False,
}

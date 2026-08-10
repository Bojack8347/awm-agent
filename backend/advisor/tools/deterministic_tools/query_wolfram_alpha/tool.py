"""Agent declaration for the bounded Wolfram|Alpha fallback."""

TOOL_SPEC = {
    "name": "query_wolfram_alpha",
    "capability": "external_math_lookup",
    "description": (
        "Last-resort external computation for a short, de-identified pure-math query "
        "that the local financial calculation plan cannot represent. Never include "
        "Client File facts, analysis identifiers, personal data, currency, financial "
        "amounts, tax or benefit rules, model assumptions, or recommendations. Declare "
        "the expected scalar unit. If this tool rejects or cannot validate the query, "
        "report the calculation capability gap instead of retrying or calculating in prose."
    ),
    "writeback_target": "none",
    "read_only": True,
}


PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "maxLength": 300,
            "description": (
                "A de-identified pure-math expression or equation. Use generic short "
                "variables only; never include client or financial data."
            ),
        },
        "expected_unit": {
            "type": "string",
            "enum": [
                "unitless",
                "decimal",
                "percentage",
                "count",
                "years",
                "months",
            ],
            "description": "The exact scalar unit expected in the validated result.",
        },
    },
    "required": ["query", "expected_unit"],
    "additionalProperties": False,
}

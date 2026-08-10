"""Agent declaration for comparing two immutable quantitative analyses."""

TOOL_SPEC = {
    "name": "compare_quant_analyses",
    "capability": "calculation_toolkit",
    "description": (
        "Compare two completed immutable cash-flow analyses or two completed immutable "
        "asset-allocation analyses. Returns exact unit-matched arithmetic deltas and input "
        "lineage without rerunning either engine. Use only analysis IDs returned by AWM; "
        "the tool does not claim that one changed input caused an outcome. When metric_keys "
        "is null, it returns a bounded set of decision-useful headline and composition metrics."
    ),
    "writeback_target": "none",
    "read_only": True,
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "base_analysis_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160,
        },
        "comparison_analysis_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160,
        },
        "domain": {
            "type": "string",
            "enum": ["cashflow", "asset_allocation"],
        },
        "metric_keys": {
            "anyOf": [
                {
                    "type": "array",
                    "maxItems": 30,
                    "items": {"type": "string", "minLength": 1, "maxLength": 160},
                },
                {"type": "null"},
            ],
            "description": (
                "Optional exact metric keys to compare, or null for the bounded "
                "decision-useful defaults for the selected domain."
            ),
        },
    },
    "required": [
        "base_analysis_id",
        "comparison_analysis_id",
        "domain",
        "metric_keys",
    ],
    "additionalProperties": False,
}

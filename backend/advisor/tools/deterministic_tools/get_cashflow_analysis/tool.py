"""Agent tool declaration for retrieving a completed cash-flow analysis."""

from __future__ import annotations

from advisor.tools.deterministic_tools.run_cashflow_projection.tool import (
    DETAIL_REPORT_COLUMNS,
)


TOOL_SPEC = {
    "name": "get_cashflow_analysis",
    "capability": "cashflow_retrieval",
    "description": (
        "Retrieve a completed validated cash-flow analysis for the current client without "
        "rerunning the model. Use this for follow-up questions about prior results, their "
        "percentiles, timing, assumptions, limitations, or implications. It can return a "
        "bounded exact-year percentile excerpt from annual series that were collected in "
        "the original run. Pass null to use the latest analysis in the current conversation, "
        "or pass an analysis_id returned by run_cashflow_projection. Do not use it for a "
        "changed what-if scenario or claim an uncollected report column."
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
            "description": "A prior cash-flow analysis_id, or null for the latest in this conversation.",
        },
        "calendar_years": {
            "anyOf": [
                {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 12,
                    "items": {
                        "type": "integer",
                        "minimum": 1900,
                        "maximum": 2200,
                    },
                },
                {"type": "null"},
            ],
            "description": (
                "Exact calendar years to excerpt from stored annual percentile "
                "series, or null for the standard stored summary."
            ),
        },
        "detail_columns": {
            "anyOf": [
                {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {
                        "type": "string",
                        "enum": DETAIL_REPORT_COLUMNS,
                    },
                },
                {"type": "null"},
            ],
            "description": (
                "LifeModel column names to return for calendar_years, or null "
                "for Net Worth, Cashflow Shortfall Debt, and Bank Balance."
            ),
        },
    },
    "required": ["analysis_id", "calendar_years", "detail_columns"],
    "additionalProperties": False,
}

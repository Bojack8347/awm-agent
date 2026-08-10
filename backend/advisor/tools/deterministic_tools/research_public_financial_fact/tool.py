"""Agent declaration for governed session-scoped public fact research."""


TOOL_SPEC = {
    "name": "research_public_financial_fact",
    "capability": "public_fact_research",
    "description": (
        "Research one exact, time-sensitive public financial fact only after the "
        "Financial Planning agent makes one of two decisions: (1) a current Client "
        "File projection/readiness result needs a supported public model input, the "
        "user cannot provide it or its configured default is absent, and the exact "
        "value is needed to proceed; or (2) a public fact is necessary to answer the "
        "user's conversational follow-up. Submit only a supported canonical variable "
        "and effective year. The server sends no Client File data, searches only the "
        "configured official government authority, and returns the model-ready value, "
        "unit, year, and citations. It validates and session-binds the result for "
        "immediate reporting and eligible local calculation without human approval. "
        "Afterward, independently decide storage/reuse with "
        "review_public_fact_reuse. Do not use for client facts, agent-selected "
        "planning assumptions, market forecasts, recommendations, facts already "
        "trusted, arbitrary URLs, or general web research."
    ),
    "writeback_target": "assumption_registry",
    "read_only": False,
}


PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "variable_key": {
            "type": "string",
            "enum": [
                "federal_standard_deduction",
                "federal_tax_brackets",
                "retirement_contribution_limits",
                "social_security_cola",
                "social_security_taxable_maximum",
                "medicare_part_b_premium",
            ],
            "description": "The exact supported public-authoritative variable needed.",
        },
        "effective_year": {
            "type": "integer",
            "minimum": 2000,
            "maximum": 2200,
            "description": "The exact calendar year required by the follow-up.",
        },
    },
    "required": ["variable_key", "effective_year"],
    "additionalProperties": False,
}

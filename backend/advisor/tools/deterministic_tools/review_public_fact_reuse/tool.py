"""Agent declaration for public-fact storage and reuse review."""


TOOL_SPEC = {
    "name": "review_public_fact_reuse",
    "capability": "public_fact_reuse_review",
    "description": (
        "Review one exact session public fact returned by "
        "research_public_financial_fact and decide whether storing and reusing it "
        "is appropriate. Choose authorize_durable_reuse only with reason "
        "authoritative_fact_reuse_appropriate; choose keep_session_only only with "
        "reason authoritative_fact_reuse_not_appropriate. The server resolves the "
        "fact from the current session and mechanically validates the decision, "
        "source, units, freshness, and durable write. This review does not route "
        "research, projection, or recommendation work. Session reporting never "
        "requires human approval."
    ),
    "writeback_target": "assumption_registry",
    "read_only": False,
}


PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "session_fact_id": {
            "type": "string",
            "pattern": "^session-public-fact:[a-f0-9]{32}$",
            "description": (
                "The opaque identifier returned by research_public_financial_fact "
                "in the current authenticated session."
            ),
        },
        "decision": {
            "type": "string",
            "enum": ["authorize_durable_reuse", "keep_session_only"],
            "description": (
                "The Financial Planning agent's semantic decision about durable "
                "storage and reuse."
            ),
        },
        "reason_code": {
            "type": "string",
            "enum": [
                "authoritative_fact_reuse_appropriate",
                "authoritative_fact_reuse_not_appropriate",
            ],
            "description": (
                "Use authoritative_fact_reuse_appropriate only with "
                "authorize_durable_reuse; otherwise use "
                "authoritative_fact_reuse_not_appropriate with keep_session_only."
            ),
        },
    },
    "required": ["session_fact_id", "decision", "reason_code"],
    "additionalProperties": False,
}

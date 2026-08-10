"""Agent tool declaration for draft_fact."""

from __future__ import annotations

from client_file.fact_vocabulary import (
    CONFIDENCE_LEVELS,
    FACT_TYPES,
    fact_properties_schema,
    entity_collection_schema,
)


TOOL_SPEC = {
    "name": "draft_fact",
    "capability": "client_file_facts",
    "read_only": False,
    "description": (
        "Store a candidate Client File fact without making it trusted knowledge. Use for "
        "high-impact, approximate, changed, ambiguous, or conflicting facts that require "
        "client confirmation before commit_facts."
    ),
    "writeback_target": "client_file.draft_facts",
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "fact_type": {
            "type": "string",
            "enum": list(FACT_TYPES),
            "description": "Most specific canonical Client File category for the candidate facts.",
        },
        "facts": {
            **fact_properties_schema(),
            "description": "Canonical structured fact fields awaiting client confirmation.",
        },
        "entities": {
            **entity_collection_schema(),
            "description": "Typed account/holding entities awaiting one scoped confirmation.",
        },
        "confidence": {
            "type": "string",
            "enum": list(CONFIDENCE_LEVELS),
            "description": "How directly and precisely the client supplied the candidate fact.",
        },
        "metadata": {
            "type": "object",
            "description": "Optional source-message and observation provenance; never place financial facts here.",
            "properties": {
                "source": {"type": "string"},
                "source_message_id": {"type": "string"},
                "observed_at": {"type": "string"},
                "note": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    "required": ["fact_type"],
    "anyOf": [{"required": ["facts"]}, {"required": ["entities"]}],
    "additionalProperties": False,
}

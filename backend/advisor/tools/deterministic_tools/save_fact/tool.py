"""Agent tool declaration for save_fact."""

from __future__ import annotations

from client_file.fact_vocabulary import (
    CONFIDENCE_LEVELS,
    FACT_TYPES,
    fact_properties_schema,
    entity_collection_schema,
)


TOOL_SPEC = {
    "name": "save_fact",
    "capability": "client_file_facts",
    "read_only": False,
    "description": (
        "Save a clear, explicit, low-risk client fact directly as trusted Client File "
        "knowledge. Use only when the fact does not require confirmation; use draft_fact "
        "for high-impact, approximate, changed, ambiguous, or conflicting facts."
    ),
    "writeback_target": "client_file.facts",
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "fact_type": {
            "type": "string",
            "enum": list(FACT_TYPES),
            "description": "Most specific canonical Client File category for the supplied facts.",
        },
        "facts": {
            **fact_properties_schema(),
            "description": "Canonical structured fact fields supported by the Client File vocabulary.",
        },
        "entities": entity_collection_schema(),
        "confidence": {
            "type": "string",
            "enum": list(CONFIDENCE_LEVELS),
            "description": "How directly and precisely the client supplied the fact.",
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

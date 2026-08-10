"""Agent tool declaration for commit_facts."""

from __future__ import annotations

from client_file.fact_vocabulary import (
    CONFIDENCE_LEVELS,
    FACT_TYPES,
    fact_properties_schema,
    entity_collection_schema,
)


TOOL_SPEC = {
    "name": "commit_facts",
    "capability": "client_file_facts",
    "read_only": False,
    "description": (
        "Commit selected draft facts or semantically confirmed/corrected current-turn facts into "
        "trusted Client File knowledge. The model decides the meaning from authenticated turn "
        "context; confirmation_text is retained as audit evidence, not matched by a phrase gate."
    ),
    "writeback_target": "client_file.facts",
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "fact_type": {
            "type": "string",
            "enum": list(FACT_TYPES),
            "description": "Most specific canonical category for facts supplied in this confirmed turn.",
        },
        "facts": {
            **fact_properties_schema(),
            "description": "Canonical structured values explicitly confirmed or corrected in the current turn.",
        },
        "entities": {
            **entity_collection_schema(),
            "description": "Explicit current-turn corrections to typed account/holding entities.",
        },
        "confirmation_action_id": {
            "type": "string",
            "description": "Stable authenticated confirmation action ID reused on retry.",
        },
        "fact_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Existing draft IDs or canonical field names to commit; never invent an ID.",
        },
        "confirmation_text": {
            "type": "string",
            "description": "The current user's confirmation text. Runtime authentication, not this argument alone, authorizes commit.",
        },
        "post_commit_action": {
            "type": "string",
            "enum": ["cashflow_projection"],
            "description": (
                "Optional agent-authored completion intent. Set this only when the current "
                "conversational request still requires a cash-flow projection or refresh after "
                "these facts are committed. Omit it for a profile-only fact update. The server "
                "does not infer this value from keywords."
            ),
        },
        "confidence": {
            "type": "string",
            "enum": list(CONFIDENCE_LEVELS),
            "description": "How directly and precisely the client confirmed the fact.",
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
    "additionalProperties": False,
}

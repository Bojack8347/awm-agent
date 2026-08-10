"""Agent tool declaration for exact conversation-history retrieval."""

from __future__ import annotations


TOOL_SPEC = {
    "name": "retrieve_conversation_history",
    "capability": "conversation_history_retrieval",
    "read_only": True,
    "description": (
        "Retrieve exact original user and advisor messages from the current client's "
        "current conversation. Use message ids or a covered range when known; use text "
        "search when the client refers to something said or promised earlier. Results "
        "may include a small surrounding context window."
    ),
    "writeback_target": "none",
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "message_ids": {
            "anyOf": [
                {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {"type": "string", "minLength": 1, "maxLength": 100},
                },
                {"type": "null"},
            ],
            "description": "Exact source message ids to retrieve, or null.",
        },
        "from_message_id": {
            "anyOf": [
                {"type": "string", "minLength": 1, "maxLength": 100},
                {"type": "null"},
            ],
            "description": "First message id of an inclusive covered range, or null.",
        },
        "through_message_id": {
            "anyOf": [
                {"type": "string", "minLength": 1, "maxLength": 100},
                {"type": "null"},
            ],
            "description": "Last message id of an inclusive covered range, or null.",
        },
        "query": {
            "anyOf": [
                {"type": "string", "minLength": 1, "maxLength": 500},
                {"type": "null"},
            ],
            "description": "Full-text search terms from the client's current request, or null.",
        },
        "max_results": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "Maximum matching messages to return. Use 20 unless more are needed.",
        },
        "context_window": {
            "type": "integer",
            "minimum": 0,
            "maximum": 10,
            "description": "Number of surrounding messages to return on each side. Use 2 normally.",
        },
    },
    "required": [
        "message_ids",
        "from_message_id",
        "through_message_id",
        "query",
        "max_results",
        "context_window",
    ],
    "additionalProperties": False,
}

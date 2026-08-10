"""Agent tool declaration for save_consultation_checkpoint."""

from __future__ import annotations


TOOL_SPEC = {
    "name": "save_consultation_checkpoint",
    "capability": "consultation_checkpoint",
    "read_only": False,
    "description": (
        "Save resumable consultation state when the client pauses, defers, or leaves material "
        "inputs unresolved. Record the active workflow phase, remaining slots, and one next "
        "question; this does not confirm facts or complete the workflow."
    ),
    "writeback_target": "client_file.consultation_checkpoints",
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "skill": {
            "type": "string",
            "description": "Exact active AWM skill ID owning this consultation.",
        },
        "phase": {
            "type": "string",
            "description": "Stable workflow phase from which the consultation should resume.",
        },
        "next_question": {
            "type": "string",
            "description": "One concise client-facing question that would move the workflow forward.",
        },
        "pending_slots": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Canonical unresolved fields or decisions still needed.",
        },
        "status": {
            "type": "string",
            "description": "Current checkpoint state, such as in_progress, paused, or deferred.",
        },
    },
    "required": ["phase"],
    "additionalProperties": True,
}

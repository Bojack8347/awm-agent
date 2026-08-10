"""Agent tool declaration for update_objective_status."""

from __future__ import annotations


TOOL_SPEC = {
    "name": "update_objective_status",
    "capability": "objective_tracking",
    "read_only": False,
    "description": (
        "Update the lifecycle state of one existing, exactly identified Client File objective. "
        "Use for workflow progress, deferral, completion, or reactivation; never mark blocked or "
        "unfinished work complete and never invent an objective ID."
    ),
    "writeback_target": "client_file.objectives",
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "objective_id": {
            "type": "string",
            "description": "Exact existing objective ID from trusted Client File or workflow context.",
        },
        "status": {
            "type": "string",
            "description": "Lifecycle state justified by the actual workflow result, such as in_progress, deferred, completed, or reactivated.",
        },
        "reason": {
            "type": "string",
            "description": "Concise evidence-backed reason for this state transition.",
        },
    },
    "required": ["status"],
    "additionalProperties": True,
}

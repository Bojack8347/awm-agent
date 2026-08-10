"""Agent tool declaration for cancelling a durable specialist job."""

from __future__ import annotations


TOOL_SPEC = {
    "name": "cancel_specialist_job",
    "capability": "specialist_job_control",
    "read_only": False,
    "description": (
        "Cancel an in-flight specialist job for the current client when the client "
        "asks to stop or changes direction."
    ),
    "writeback_target": "agent.specialist_jobs",
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "job_id": {
            "type": "string",
            "description": "Durable specialist job id shown in the Specialist jobs prompt section.",
        },
        "reason": {
            "type": "string",
            "description": "Why the client or advisor cancelled the work.",
        },
    },
    "required": ["job_id", "reason"],
    "additionalProperties": False,
}

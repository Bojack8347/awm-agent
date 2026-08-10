"""Agent tool declaration for record_policy_review_outcome."""

from __future__ import annotations


TOOL_SPEC = {
    "name": "record_policy_review_outcome",
    "capability": "policy_review_outcome",
    "read_only": False,
    "description": (
        "Record a clear client decision about one specifically identified proposal or active "
        "policy. Use only when the authenticated current user message clearly expresses approve, "
        "refine, defer, or keep unchanged. This records advice review, not execution consent."
    ),
    "writeback_target": "client_file.policies",
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "target_type": {
            "type": "string",
            "enum": ["proposal", "policy"],
            "description": "Whether the identified review target is a proposed artifact or an active policy.",
        },
        "target_id": {
            "type": "string",
            "description": "Exact proposal or policy ID. Omit only when trusted context contains exactly one unambiguous eligible target.",
        },
        "decision": {
            "type": "string",
            "enum": ["approve", "refine", "defer", "keep_unchanged"],
            "description": "Client's explicit review decision; keep_unchanged applies to an active policy, not a new proposal.",
        },
        "rationale": {
            "type": "string",
            "description": "Client-stated reason or requested refinement, without invented justification.",
        },
    },
    "required": ["target_type", "decision"],
    "additionalProperties": False,
}

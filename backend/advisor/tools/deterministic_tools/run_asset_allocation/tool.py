"""Agent tool declaration for run_asset_allocation."""

from __future__ import annotations


TOOL_SPEC = {
    "name": "run_asset_allocation",
    "capability": "allocation_construction",
    "description": (
        "Compute a read-only asset allocation for one durably signed investment "
        "assessment. Supply only the signed assessment identity; the server resolves "
        "all mandate inputs from the current Client File. Use for securities, dollar "
        "allocations, percentages, expected return, and expected risk. Its successful result "
        "already contains the immutable analysis evidence; finish the current specialist call "
        "from that result instead of retrieving or rerunning it."
    ),
    "writeback_target": "none",
    "read_only": True,
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "assessment_ref": {
            "type": "object",
            "description": (
                "Identity of the durable signed assessment. Financial mandate fields "
                "are resolved server-side and cannot be supplied or overridden here."
            ),
            "properties": {
                "assessment_id": {"type": "string", "minLength": 1},
                "assessment_version": {"type": "integer", "minimum": 1},
                "money_pool_id": {"type": "string", "minLength": 1},
            },
            "required": ["assessment_id", "assessment_version", "money_pool_id"],
            "additionalProperties": False,
        },
    },
    "required": ["assessment_ref"],
    "additionalProperties": False,
}

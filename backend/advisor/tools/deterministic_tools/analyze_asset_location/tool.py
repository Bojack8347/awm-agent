"""Agent declaration for simplified tax-aware asset location."""

TOOL_SPEC = {
    "name": "analyze_asset_location",
    "capability": "portfolio_analytics",
    "description": (
        "Map a completed allocation across confirmed taxable brokerage and retirement "
        "account capacity using a versioned tax-efficiency ordering. Preserves total "
        "asset-class weights and returns reporting-only placement analysis; it is not "
        "tax preparation or a trade recommendation."
    ),
    "writeback_target": "none",
    "read_only": True,
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "allocation_analysis_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160,
        },
    },
    "required": ["allocation_analysis_id"],
    "additionalProperties": False,
}

"""Agent tool declaration for upsert_money_pool."""

from __future__ import annotations


TOOL_SPEC = {
    "name": "upsert_money_pool",
    "capability": "money_pool_management",
    "read_only": False,
    "description": (
        "Create or update one distinct planning money pool by its stable label after purpose "
        "and amount are known. Store only client-confirmed or accepted planning inputs. This "
        "defines a pool for assessment; it does not move money, create a proposal, or execute an investment."
    ),
    "writeback_target": "client_file.money_pools",
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {
            "type": "string",
            "description": "Stable client-facing pool label; matching is case-insensitive and updates the same pool.",
        },
        "purpose": {
            "type": "string",
            "description": "Specific goal or use for this pool.",
        },
        "amount": {
            "type": "number",
            "description": "Confirmed or accepted pool amount in the client's account currency.",
        },
        "horizon_years": {
            "type": "number",
            "description": "Expected time until the client needs this pool, in years.",
        },
        "risk_tolerance": {
            "type": "string",
            "description": "Client's stated risk posture for this pool, not a household-wide risk label.",
        },
        "source_of_funds": {
            "type": "string",
            "description": "Confirmed account, holding, cash source, or other origin of the pool amount.",
        },
        "liquidity_needs": {
            "type": "string",
            "description": "Client-stated access timing or minimum liquid amount/share for this pool.",
        },
        "liquidity_constraint_mode": {
            "type": "string",
            "enum": [
                "no_additional_pool_constraint",
                "specific_in_pool_constraint",
                "unspecified",
            ],
            "description": (
                "Normalized implementation meaning of the client's liquidity preference. "
                "Use no_additional_pool_constraint when emergency liquidity is held "
                "outside this pool or no pool cash buffer is required; use "
                "specific_in_pool_constraint only when the client requires this pool "
                "itself to retain a minimum liquid amount or share."
            ),
        },
        "complexity_preference": {
            "type": "string",
            "description": "Client preference for simple, flexible, or specialized implementation; do not encode product phrases as asset-class exclusions.",
        },
        "asset_class_preferences": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Client-stated asset-class preferences using recognized asset-class names when possible.",
        },
        "exclusions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Confirmed asset-class exclusions only; do not put options, leverage, plain vanilla, or other product-style complexity phrases here.",
        },
        "special_considerations": {
            "type": "string",
            "description": "Other confirmed mandate constraints or implementation context for assessment.",
        },
        "tax_considerations": {
            "type": "string",
            "description": "Known client-stated tax context; do not invent tax calculations or advice.",
        },
    },
    "required": ["label", "purpose", "amount"],
    "additionalProperties": True,
}

"""Agent tool declaration for create_investment_assessment."""

from __future__ import annotations

from advisor.tools.deterministic_tools.investment_assessment_contract import (
    MAX_TARGET_VOLATILITY_TOLERANCE_BPS,
    SUPPORTED_COMPLEXITY_PREFERENCE,
    SUPPORTED_LIQUIDITY_REQUIREMENT,
)


TOOL_SPEC = {
    "name": "create_investment_assessment",
    "capability": "assessment_creation",
    "description": (
        "Create and durably persist a pending, versioned investment assessment for "
        "one existing Client File money pool. The server resolves amount, purpose, "
        "funding source, horizon, and risk from the current Client File. Supply every "
        "allocation-mandate field explicitly; no business defaults are inferred."
    ),
    "writeback_target": "client_file.plans",
    "read_only": False,
    "irreversible": False,
    "requires_explicit_consent": False,
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "money_pool_id": {
            "type": "string",
            "minLength": 1,
            "description": "Identity of an existing money pool in the current Client File.",
        },
        "target_volatility_annual_decimal": {
            "type": "number",
            "minimum": 0.05,
            "maximum": 0.20,
            "description": "Explicit annual volatility target as a decimal (for example, 0.10).",
        },
        "target_volatility_tolerance_bps": {
            "type": "number",
            "minimum": 0,
            "maximum": MAX_TARGET_VOLATILITY_TOLERANCE_BPS,
            "description": (
                "Explicit approved tolerance around target volatility, in basis points. "
                "The current optimizer contract supports at most 80 bps."
            ),
        },
        "active_risk_percentage": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "Explicit active-management share as a decimal from 0 to 1.",
        },
        "liquidity_requirement": {
            "type": "string",
            "enum": [SUPPORTED_LIQUIDITY_REQUIREMENT],
            "description": (
                "The optimizer does not yet enforce portfolio liquidity constraints. "
                "Only the explicit no-additional-constraint mode is supported."
            ),
        },
        "complexity_preference": {
            "type": "string",
            "enum": [SUPPORTED_COMPLEXITY_PREFERENCE],
            "description": (
                "The optimizer does not yet enforce an additional complexity limit. "
                "Only the unrestricted optimizer mode is supported. "
                "Client language such as plain vanilla, simple, no options, or no "
                "leverage maps here as optimizer_unrestricted — do not put those "
                "phrases into exclusions."
            ),
        },
        "exclusions": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": (
                "Only NEO canonical asset-class names or known aliases "
                "(for example Cash, US Equity, Bitcoin, Commodities, Hedge Funds, "
                "Gold). Use [] when the client has no asset-class exclusion, "
                "including plain-vanilla / simple / no-options / no-leverage "
                "preferences. Never pass product phrases such as options, "
                "leverage, complex products, or plain vanilla as exclusion strings."
            ),
        },
        "authorized_specialized_asset_classes": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": (
                "Only NEO specialized/gated asset-class names or known aliases "
                "authorized for consideration (for example Bitcoin, Commodities, "
                "Gold, Hedge Funds). Use [] to authorize none. Same resolvable "
                "names as exclusions — not product phrases."
            ),
        },
        "valid_until": {
            "type": "string",
            "format": "date-time",
            "minLength": 1,
            "description": "Explicit expiry timestamp for this assessment's validity.",
        },
    },
    "required": [
        "money_pool_id",
        "target_volatility_annual_decimal",
        "target_volatility_tolerance_bps",
        "active_risk_percentage",
        "liquidity_requirement",
        "complexity_preference",
        "exclusions",
        "authorized_specialized_asset_classes",
        "valid_until",
    ],
    "additionalProperties": False,
}

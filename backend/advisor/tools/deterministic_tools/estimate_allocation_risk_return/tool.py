"""Agent tool declaration for estimateAllocationRiskReturn."""

from __future__ import annotations

from advisor.tools.deterministic_tools._schema import OBJECT_SCHEMA


TOOL_SPEC = {
    "name": "estimateAllocationRiskReturn",
    "capability": "risk_return_estimate",
    "description": (
        "Estimate expected return and volatility for an explicit asset allocation or account "
        "pool using deterministic capital-market assumptions. Use for bounded assessment, "
        "diagnosis, or revalidation evidence when actual allocation/account inputs are available; "
        "do not use it to construct a portfolio or invent missing weights or assumptions."
    ),
    "writeback_target": "none",
    "read_only": True,
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "allocation": {
            **OBJECT_SCHEMA,
            "description": "Explicit asset-class-to-weight allocation. Supply this or accounts, not both.",
        },
        "accounts": {
            "type": "array",
            "items": OBJECT_SCHEMA,
            "description": "Explicit account records from trusted context. Supply this or allocation, not both.",
        },
        "default_return": {
            "type": "number",
            "description": "Explicit fallback expected return for otherwise unresolved inputs, as decimal or percent-like value; never invent it.",
        },
        "default_volatility": {
            "type": "number",
            "description": "Explicit fallback volatility for otherwise unresolved inputs, as decimal or percent-like value; never invent it.",
        },
    },
    "additionalProperties": False,
}

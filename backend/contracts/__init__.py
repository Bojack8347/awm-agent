"""Shared runtime contracts for AWM service boundaries.

LLM provider JSON schemas still live in ``llm.schemas`` because those are
adapter-facing. This package owns app/runtime payload contracts that routes,
services, agents, and tests can share.
"""

from contracts.companion import (
    CompanionMessageResponseContract,
    PersistedMessageContract,
    validate_companion_message_response,
)
from contracts.journey import (
    JourneyActivationResponseContract,
    validate_journey_activation_response,
)
from contracts.policy import (
    FinalPolicyContract,
    PolicySecurityContract,
    REQUIRED_STEP1_POLICY_FIELDS,
    REQUIRED_STEP1_SECTION_TITLES,
    STEP1_FORBIDDEN_UI_FIELDS,
    normalize_final_policy_json,
    validate_step1_policy_schema,
)
from contracts.tools import (
    AssetAllocationModelToolArgsContract,
    CashflowToolArgsContract,
    build_asset_allocation_tool_args,
    build_cashflow_tool_args,
)

__all__ = [
    "AssetAllocationModelToolArgsContract",
    "CashflowToolArgsContract",
    "CompanionMessageResponseContract",
    "FinalPolicyContract",
    "JourneyActivationResponseContract",
    "PersistedMessageContract",
    "PolicySecurityContract",
    "REQUIRED_STEP1_POLICY_FIELDS",
    "REQUIRED_STEP1_SECTION_TITLES",
    "STEP1_FORBIDDEN_UI_FIELDS",
    "build_asset_allocation_tool_args",
    "build_cashflow_tool_args",
    "normalize_final_policy_json",
    "validate_companion_message_response",
    "validate_journey_activation_response",
    "validate_step1_policy_schema",
]

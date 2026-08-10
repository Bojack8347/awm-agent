"""Backward-compatible policy contract imports for LLM code.

Runtime policy contracts now live in ``contracts.policy`` so services, routes,
agents, and tests share the same schema source.
"""

from __future__ import annotations

from contracts.policy import (
    FinalPolicyContract,
    PolicySecurityContract,
    REQUIRED_STEP1_POLICY_FIELDS,
    REQUIRED_STEP1_SECTION_TITLES,
    STEP1_FORBIDDEN_UI_FIELDS,
    normalize_final_policy_json,
    validate_step1_policy_schema,
)


__all__ = [
    "FinalPolicyContract",
    "PolicySecurityContract",
    "REQUIRED_STEP1_POLICY_FIELDS",
    "REQUIRED_STEP1_SECTION_TITLES",
    "STEP1_FORBIDDEN_UI_FIELDS",
    "normalize_final_policy_json",
    "validate_step1_policy_schema",
]

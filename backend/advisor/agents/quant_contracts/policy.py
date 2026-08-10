from __future__ import annotations

import os
from typing import Any, Dict


def quant_recommendations_enabled(*, env_getter: Any = os.getenv) -> bool:
    """Whether recommendation narration may pass after every evidence gate succeeds.

    This flag is containment, not validation.  Enabling it never overrides an
    invalid result, failed constraint, unsupported number, or missing warning.
    """

    return quant_recommendation_policy(env_getter=env_getter)["enabled"] is True


def quant_recommendation_policy(*, env_getter: Any = os.getenv) -> Dict[str, Any]:
    """Resolve the versioned, fail-closed client-narration policy manifest."""

    flag = str(
        env_getter("AWM_QUANT_RECOMMENDATIONS_ENABLED", "") or ""
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    policy_id = str(
        env_getter("AWM_QUANT_RECOMMENDATION_POLICY_ID", "") or ""
    ).strip()
    policy_version = str(
        env_getter("AWM_QUANT_RECOMMENDATION_POLICY_VERSION", "") or ""
    ).strip()
    approval_status = str(
        env_getter("AWM_QUANT_RECOMMENDATION_POLICY_APPROVAL_STATUS", "")
        or "not_supplied"
    ).strip().lower()
    enabled = bool(
        flag
        and policy_id
        and policy_version
        and approval_status == "approved"
    )
    blockers = []
    if not flag:
        blockers.append("recommendation_feature_flag_disabled")
    if not policy_id:
        blockers.append("approved_policy_id_required")
    if not policy_version:
        blockers.append("approved_policy_version_required")
    if approval_status != "approved":
        blockers.append("policy_approval_status_must_be_approved")
    return {
        "schema_version": "awm.quant_recommendation_policy.v1",
        "policy_id": policy_id or None,
        "policy_version": policy_version or None,
        "approval_status": approval_status,
        "feature_flag_enabled": flag,
        "enabled": enabled,
        "blockers": blockers,
        "scope": "client_facing_quantitative_recommendation_narration",
    }

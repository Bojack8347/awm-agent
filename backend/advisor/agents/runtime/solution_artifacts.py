from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from advisor.agents.runtime._shared import _runtime_number


def _investment_solution_artifact_from_allocation_writeback(
    *,
    tool_result: Dict[str, Any],
    proposal_writeback: Dict[str, Any],
    client_file: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    policy_row = proposal_writeback.get("policy") if isinstance(proposal_writeback.get("policy"), dict) else {}
    policy_payload = policy_row.get("payload") if isinstance(policy_row.get("payload"), dict) else {}
    app_artifact = proposal_writeback.get("artifact") if isinstance(proposal_writeback.get("artifact"), dict) else {}
    app_payload = app_artifact.get("payload") if isinstance(app_artifact.get("payload"), dict) else {}
    if not policy_payload and not app_payload:
        return None

    arguments = tool_result.get("arguments") if isinstance(tool_result.get("arguments"), dict) else {}
    assessment_id = str(
        arguments.get("assessment_id")
        or policy_payload.get("assessment_id")
        or app_payload.get("assessment_id")
        or ""
    ).strip()
    money_pool_id = str(
        arguments.get("money_pool_id")
        or policy_payload.get("money_pool_id")
        or app_payload.get("money_pool_id")
        or ""
    ).strip()
    assessment = _find_client_file_assessment(client_file, assessment_id=assessment_id, money_pool_id=money_pool_id)
    source_assessment = _source_assessment_from_client_file_assessment(
        assessment,
        fallback_assessment_id=assessment_id,
        fallback_money_pool_id=money_pool_id,
    )
    money_pool = (
        policy_payload.get("money_pool") if isinstance(policy_payload.get("money_pool"), dict) else {}
    ) or (
        app_payload.get("money_pool") if isinstance(app_payload.get("money_pool"), dict) else {}
    ) or _find_client_file_money_pool(client_file, money_pool_id)
    portfolio_analytics = (
        policy_payload.get("portfolio_analytics")
        if isinstance(policy_payload.get("portfolio_analytics"), dict)
        else {}
    )
    policy = policy_payload.get("policy") if isinstance(policy_payload.get("policy"), dict) else {}
    target_allocation = policy.get("target_allocation") if isinstance(policy.get("target_allocation"), dict) else {}
    recommended_securities = (
        policy_payload.get("recommended_securities")
        if isinstance(policy_payload.get("recommended_securities"), list)
        else []
    )
    amount = _runtime_number(money_pool.get("amount") or arguments.get("total_investment"))
    proposal_id = (
        f"proposal-{assessment_id}"
        if assessment_id
        else str(policy_payload.get("proposal_id") or app_payload.get("source_advisor_artifact_id") or app_artifact.get("id") or "")
    )
    if not proposal_id:
        return None
    investment_consultation_id = source_assessment.get("investment_consultation_id")
    payload = {
        "id": proposal_id,
        "schema_version": "investment_policy_proposal.v1",
        "artifact_status": "ready",
        "artifact_type": "investment_policy_proposal",
        "status": "drafted",
        "policy_operation": "propose_new_policy",
        "source": "investment_solution_agent_sdk",
        "objective": {},
        "source_assessment": source_assessment,
        "investment_consultation_id": investment_consultation_id,
        "money_pool": money_pool,
        "policy": {
            "title": app_payload.get("title") or policy_payload.get("title") or "Investment Policy Proposal",
            "version": 1,
            "policy_version": 1,
            "status": "proposed",
            "review_status": "pending",
            "capital_required": amount,
            "scope_of_purpose": _policy_scope_from_money_pool(money_pool),
            "risk_profile": money_pool.get("risk_tolerance"),
            "horizon_years": _horizon_years_from_money_pool(money_pool),
            "expected_return": portfolio_analytics.get("expected_return"),
            "expected_volatility": portfolio_analytics.get("expected_volatility"),
            "source_investment_consultation_id": investment_consultation_id,
            "source_assessment_id": source_assessment.get("assessment_id"),
            "source_assessment_version": source_assessment.get("assessment_version"),
            "target_allocation": target_allocation,
            "recommended_securities": recommended_securities,
        },
        "portfolio_analytics": portfolio_analytics,
        "engine_run": policy_payload.get("engine_run") if isinstance(policy_payload.get("engine_run"), dict) else {},
        "missing_data": [],
        "proposal_writeback": proposal_writeback,
    }
    return {
        "artifact_type": "investment_solution_policy",
        "payload": payload,
        "writeback_target": "client_file.policies",
    }


def _find_client_file_assessment(
    client_file: Dict[str, Any],
    *,
    assessment_id: str,
    money_pool_id: str,
) -> Dict[str, Any]:
    candidates: List[Dict[str, Any]] = []
    if isinstance(client_file, dict):
        for key in ("investment_assessments", "signed_investment_assessments", "assessments"):
            candidates.extend(_dict_items(client_file.get(key)))
        artifacts = client_file.get("artifacts") if isinstance(client_file.get("artifacts"), dict) else {}
        candidates.extend(_dict_items(artifacts.get("plans")))
    for candidate in candidates:
        payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else candidate
        if assessment_id and str(payload.get("assessment_id") or payload.get("id") or "") != assessment_id:
            continue
        if money_pool_id:
            basis = _assessment_basis(payload)
            payload_pool_id = str(payload.get("money_pool_id") or basis.get("money_pool_id") or "")
            if payload_pool_id and payload_pool_id != money_pool_id:
                continue
        return payload
    return {}


def _source_assessment_from_client_file_assessment(
    assessment: Dict[str, Any],
    *,
    fallback_assessment_id: str,
    fallback_money_pool_id: str,
) -> Dict[str, Any]:
    basis = _assessment_basis(assessment)
    return {
        "schema_version": "investment_assessment.v1",
        "investment_consultation_id": (
            assessment.get("investment_consultation_id")
            or basis.get("investment_consultation_id")
        ),
        "assessment_id": assessment.get("assessment_id") or assessment.get("id") or fallback_assessment_id,
        "assessment_version": assessment.get("assessment_version") or assessment.get("version") or 1,
        "money_pool_id": assessment.get("money_pool_id") or basis.get("money_pool_id") or fallback_money_pool_id,
        "status": "signed_off",
        "signed_off_at": assessment.get("signed_off_at"),
    }


def _assessment_basis(assessment: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(assessment, dict):
        return {}
    if isinstance(assessment.get("consultation_basis"), dict):
        return assessment["consultation_basis"]
    nested = assessment.get("assessment") if isinstance(assessment.get("assessment"), dict) else {}
    if isinstance(nested.get("basis"), dict):
        return nested["basis"]
    if isinstance(assessment.get("basis"), dict):
        return assessment["basis"]
    return {}


def _find_client_file_money_pool(client_file: Dict[str, Any], money_pool_id: str) -> Dict[str, Any]:
    if not isinstance(client_file, dict):
        return {}
    for pool in _dict_items(client_file.get("money_pools")):
        if str(pool.get("id") or "") == str(money_pool_id or ""):
            return pool
    return {}


def _policy_scope_from_money_pool(money_pool: Dict[str, Any]) -> str:
    amount = _runtime_number(money_pool.get("amount"))
    amount_text = f"${amount:,.0f}" if amount is not None else "the signed capital"
    purpose = str(money_pool.get("purpose_type") or money_pool.get("purpose") or "the signed purpose")
    horizon = _horizon_years_from_money_pool(money_pool)
    horizon_text = f" over about {horizon} years" if horizon is not None else ""
    return f"Deploy {amount_text}{horizon_text} for {purpose}."


def _horizon_years_from_money_pool(money_pool: Dict[str, Any]) -> Optional[int]:
    for key in ("horizon_years", "horizon"):
        value = money_pool.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
    text = str(money_pool.get("horizon_text") or "")
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else None


def _dict_items(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        if value.get("assessment_id") or value.get("schema_version") or value.get("artifact_type"):
            return [value]
        rows = value.get("items") if isinstance(value.get("items"), list) else None
        if rows is not None:
            return [item for item in rows if isinstance(item, dict)]
        return [item for item in value.values() if isinstance(item, dict)]
    return []

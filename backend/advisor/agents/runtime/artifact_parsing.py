from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from advisor.agents.runtime._shared import _runtime_number, _subagent_artifact_key


def _extract_subagent_artifacts(raw_output: Any) -> List[Dict[str, Any]]:
    artifacts: List[Dict[str, Any]] = []
    for value in _json_values_from_tool_output(raw_output):
        artifacts.extend(_subagent_artifacts_from_value(value))
    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for artifact in artifacts:
        key = _subagent_artifact_key(artifact)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(artifact)
    return deduped


def _json_values_from_tool_output(raw_output: Any) -> List[Any]:
    if raw_output is None:
        return []
    if isinstance(raw_output, (dict, list)):
        return [raw_output]
    if isinstance(raw_output, bytes):
        raw_output = raw_output.decode("utf-8", errors="ignore")
    if not isinstance(raw_output, str):
        return []
    text = raw_output.strip()
    if not text:
        return []

    candidates = [text]
    if text.upper().startswith("FINALIZE:"):
        candidates.append(text.split(":", 1)[1].strip())
    candidates.extend(
        match.group(1).strip()
        for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    )

    values: List[Any] = []
    decoder = json.JSONDecoder()
    seen_serialized: set[str] = set()
    for candidate in candidates:
        for value in _decode_json_candidates(candidate, decoder):
            marker = json.dumps(value, sort_keys=True, default=str)
            if marker in seen_serialized:
                continue
            seen_serialized.add(marker)
            values.append(value)
    return values


def _decode_json_candidates(text: str, decoder: json.JSONDecoder) -> List[Any]:
    values: List[Any] = []
    try:
        values.append(json.loads(text))
    except json.JSONDecodeError:
        pass
    for index, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        values.append(value)
        break
    return values


def _subagent_artifacts_from_value(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        artifacts: List[Dict[str, Any]] = []
        for item in value:
            artifacts.extend(_subagent_artifacts_from_value(item))
        return artifacts
    if not isinstance(value, dict):
        return []

    artifacts: List[Dict[str, Any]] = []
    nested = value.get("subagent_artifacts")
    if isinstance(nested, list):
        artifacts.extend(_subagent_artifacts_from_value(nested))

    artifact = _normalize_subagent_artifact(value)
    if artifact is not None:
        artifacts.append(artifact)
    elif isinstance(value.get("payload"), dict):
        artifacts.extend(_subagent_artifacts_from_value(value["payload"]))
    return artifacts


def _normalize_subagent_artifact(value: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    artifact_type = str(value.get("artifact_type") or "").strip()
    payload = value.get("payload") if isinstance(value.get("payload"), dict) else None
    if artifact_type in {
        "financial_planning_analysis",
        "investment_solution_policy",
        "investment_solution_policy_exit",
    } and payload is not None:
        artifact = dict(value)
        if "writeback_target" not in artifact:
            artifact["writeback_target"] = (
                "client_file.policies"
                if artifact_type.startswith("investment_solution")
                else "client_file.plans"
            )
        return artifact
    if _is_raw_investment_policy(value):
        return _wrap_raw_investment_policy(value)
    if _is_raw_investment_assessment(value):
        return {
            "artifact_type": "financial_planning_analysis",
            "payload": dict(value),
            "writeback_target": "client_file.plans",
        }
    return None


def _is_raw_investment_policy(value: Dict[str, Any]) -> bool:
    return (
        value.get("schema_version") == "investment_policy_proposal.v1"
        or value.get("artifact_type") == "investment_policy_proposal"
        or bool(value.get("proposal_id") and value.get("recommended_securities"))
    )


def _is_raw_investment_assessment(value: Dict[str, Any]) -> bool:
    return (
        value.get("schema_version") == "investment_assessment.v1"
        or value.get("artifact_type") == "investment_assessment"
        or value.get("analysis_type") == "internal_investment_assessment"
    )


def _wrap_raw_investment_policy(policy: Dict[str, Any]) -> Dict[str, Any]:
    assessment_id = str(policy.get("assessment_id") or "").strip()
    proposal_id = str(
        policy.get("proposal_id")
        or policy.get("id")
        or (f"proposal-{assessment_id}" if assessment_id else "proposal-current")
    )
    amount = _runtime_number(policy.get("capital_required") or policy.get("amount"))
    recommended = policy.get("recommended_securities") if isinstance(policy.get("recommended_securities"), list) else []
    source_assessment = {
        "schema_version": "investment_assessment.v1",
        "investment_consultation_id": policy.get("investment_consultation_id"),
        "assessment_id": assessment_id,
        "assessment_version": policy.get("assessment_version") or 1,
        "money_pool_id": policy.get("money_pool_id"),
        "status": "signed_off",
        "signed_off_at": policy.get("signed_off_at"),
    }
    missing_data = policy.get("missing_data") if isinstance(policy.get("missing_data"), list) else []
    status = str(policy.get("status") or "proposed").strip().lower()
    artifact_status = "blocked" if missing_data or status in {"blocked", "missing_data"} else "ready"
    payload = {
        "id": proposal_id,
        "schema_version": "investment_policy_proposal.v1",
        "artifact_status": artifact_status,
        "artifact_type": "investment_policy_proposal",
        "status": "drafted" if artifact_status == "ready" else status,
        "policy_operation": "propose_new_policy",
        "source": "investment_solution_agent_sdk",
        "objective": {},
        "source_assessment": source_assessment,
        "investment_consultation_id": policy.get("investment_consultation_id"),
        "money_pool": {
            "id": policy.get("money_pool_id"),
            "label": policy.get("money_pool_label") or policy.get("title") or "Money pool",
            "amount": amount,
            "horizon_years": policy.get("horizon_years"),
        },
        "policy": {
            "title": policy.get("title") or "Investment Policy Proposal",
            "version": policy.get("policy_version") or 1,
            "policy_version": policy.get("policy_version") or 1,
            "status": "proposed",
            "review_status": "pending",
            "capital_required": amount,
            "scope_of_purpose": policy.get("scope_of_purpose"),
            "horizon_years": policy.get("horizon_years"),
            "expected_return": policy.get("expected_return"),
            "expected_volatility": policy.get("expected_volatility") or policy.get("expected_risk"),
            "source_investment_consultation_id": policy.get("investment_consultation_id"),
            "source_assessment_id": assessment_id,
            "source_assessment_version": policy.get("assessment_version") or 1,
            "target_allocation": _allocation_from_recommended_securities(recommended, total_amount=amount),
            "recommended_securities": recommended,
        },
        "portfolio_analytics": {
            "expected_return": policy.get("expected_return"),
            "expected_volatility": policy.get("expected_volatility") or policy.get("expected_risk"),
            "source": "investment_solution_agent_sdk",
        },
        "engine_run": policy.get("engine_run") if isinstance(policy.get("engine_run"), dict) else {},
        "missing_data": missing_data,
        "specialist_output": dict(policy),
    }
    return {
        "artifact_type": "investment_solution_policy",
        "payload": payload,
        "writeback_target": "client_file.policies",
    }


def _allocation_from_recommended_securities(
    securities: List[Any],
    *,
    total_amount: Optional[float],
) -> Dict[str, float]:
    allocation: Dict[str, float] = {}
    for security in securities:
        if not isinstance(security, dict):
            continue
        label = str(
            security.get("asset_class")
            or security.get("recommended_security")
            or security.get("symbol")
            or security.get("ticker")
            or ""
        ).strip()
        if not label:
            continue
        weight = _runtime_number(security.get("weight"))
        if weight is None:
            percentage = _runtime_number(security.get("percentage"))
            if percentage is not None:
                weight = percentage / 100.0 if abs(percentage) > 1.0 else percentage
        if weight is None and total_amount:
            amount = _runtime_number(security.get("amount") or security.get("notional"))
            if amount is not None:
                weight = amount / total_amount
        if weight is None:
            continue
        allocation[label] = round(allocation.get(label, 0.0) + float(weight), 8)
    return allocation

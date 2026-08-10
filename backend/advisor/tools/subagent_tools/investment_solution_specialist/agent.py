"""Investment Solution sub-agent for AWM advisor.

This is a deterministic policy/proposal specialist behind the advisor sub-agent
contract. It produces structured artifacts from Client File money pools instead
of letting the Main Agent invent policy numbers in conversation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from advisor.tools.subagent_tools.common.interfaces import SubAgentArtifact
from advisor.tools.subagent_tools.investment_solution_specialist.mobile_section_5b import (
    build_mobile_section_5b,
)

if TYPE_CHECKING:
    from advisor.tools.deterministic_tools.execution import AssetAllocationModelClient


POLICY_SCHEMA_VERSION = "investment_policy_proposal.v1"
ASSESSMENT_SCHEMA_VERSION = "investment_assessment.v1"
SIGNED_ASSESSMENT_STATUSES = {"signed_off", "approved", "confirmed"}


@dataclass(frozen=True)
class InvestmentSolutionAgentV2:
    """Small deterministic IPS/proposal specialist."""

    source: str = "investment_solution_agent_v2"
    asset_allocation_client: Optional["AssetAllocationModelClient"] = None

    def build_artifact(
        self,
        *,
        client_id: str,
        objective: Dict[str, Any],
        client_file: Dict[str, Any],
    ) -> SubAgentArtifact:
        operation = _policy_operation_from_objective(objective)
        if operation == "policy_exit":
            return self.build_exit_artifact(
                client_id=client_id,
                objective=objective,
                client_file=client_file,
            )
        signed_assessment: Optional[Dict[str, Any]] = None
        assessment_ref: Dict[str, Any] = {}
        if operation == "propose_new_policy":
            signed_assessment = _select_signed_assessment(client_file=client_file, objective=objective)
            if signed_assessment is None:
                return _blocked_policy_artifact(
                    client_id=client_id,
                    objective=objective,
                    operation=operation,
                    source=self.source,
                    status="assessment_signoff_required",
                    reason="Investment Solution requires a durably signed investment assessment before proposal construction.",
                    missing_data=["signed_investment_assessment"],
                )
            assessment_ref = _assessment_ref(signed_assessment, objective=objective)
            if not assessment_ref.get("assessment_id"):
                return _blocked_policy_artifact(
                    client_id=client_id,
                    objective=objective,
                    operation=operation,
                    source=self.source,
                    status="assessment_id_required",
                    reason="Investment Solution requires a specific signed assessment id before proposal construction.",
                    missing_data=["assessment_id"],
                    assessment_ref=assessment_ref,
                )
            pool = _money_pool_from_assessment(
                signed_assessment,
                client_file=client_file,
                objective=objective,
            )
        else:
            signed_assessment = _select_signed_assessment(
                client_file=client_file,
                objective=objective,
                require_requested_id=False,
            )
            if signed_assessment is not None:
                assessment_ref = _assessment_ref(signed_assessment, objective=objective)
                pool = _money_pool_from_assessment(
                    signed_assessment,
                    client_file=client_file,
                    objective=objective,
                )
            else:
                pool = _select_money_pool(client_file=client_file, objective=objective)

        risk = _risk_from_pool(pool)
        horizon = _horizon_from_pool(pool)
        missing_inputs = _missing_policy_inputs(pool)
        if operation == "propose_new_policy" and missing_inputs:
            return _blocked_policy_artifact(
                client_id=client_id,
                objective=objective,
                operation=operation,
                source=self.source,
                status="signed_assessment_incomplete",
                reason="The signed assessment is missing policy construction inputs.",
                missing_data=missing_inputs,
                assessment_ref=assessment_ref,
            )
        asset_allocation_fields: Dict[str, Any] = {}
        engine_source = "deterministic_v2_frontier"
        expected_return: Any
        expected_volatility: Any
        allocation: Dict[str, Any]
        securities: List[Dict[str, Any]]
        engine_run: Dict[str, Any]
        if self.asset_allocation_client is not None and self.asset_allocation_client.enabled:
            from advisor.tools.deterministic_tools.execution import map_asset_allocation_result_to_policy_fields

            asset_allocation_result = self.asset_allocation_client.optimize_money_pool(pool)
            if (
                asset_allocation_result.get("success") is not True
                or asset_allocation_result.get("valid_for_recommendation") is not True
            ):
                return SubAgentArtifact(
                    artifact_type="investment_solution_policy",
                    payload={
                        "schema_version": POLICY_SCHEMA_VERSION,
                        "client_id": client_id,
                        "status": "allocation_validation_failed",
                        "artifact_status": "blocked",
                        "policy_operation": operation,
                        "source": self.source,
                        "objective": objective,
                        "source_assessment": assessment_ref,
                        "engine_run": asset_allocation_result,
                        "calculation_policy": (
                            "The asset-allocation result was unavailable or invalid for "
                            "recommendation; no deterministic frontier fallback was applied."
                        ),
                    },
                    writeback_target="client_file.policies",
                )
            asset_allocation_fields = map_asset_allocation_result_to_policy_fields(asset_allocation_result)
            engine_source = "asset_allocation_model"
        elif operation == "propose_new_policy":
            return SubAgentArtifact(
                artifact_type="investment_solution_policy",
                payload={
                    "schema_version": POLICY_SCHEMA_VERSION,
                    "client_id": client_id,
                    "status": "allocation_model_required",
                    "artifact_status": "blocked",
                    "policy_operation": operation,
                    "source": self.source,
                    "objective": objective,
                    "source_assessment": assessment_ref,
                    "calculation_policy": (
                        "A recommendation-grade asset-allocation result is required; "
                        "no deterministic frontier fallback was applied."
                    ),
                },
                writeback_target="client_file.policies",
            )
        if asset_allocation_fields:
            expected_return = asset_allocation_fields.get("expected_return")
            expected_volatility = asset_allocation_fields.get("expected_volatility")
            allocation = asset_allocation_fields.get("target_allocation") or {}
            securities = asset_allocation_fields.get("recommended_securities") or []
            engine_run = asset_allocation_fields.get("engine_run") or {}
        else:
            frontier = _frontier_for(risk=risk, horizon_years=horizon)
            allocation = _allocation_for(risk=risk, purpose=str(pool.get("purpose_type") or "growth"))
            securities = _securities_from_allocation(allocation, total_amount=_number(pool.get("amount")))
            expected_return = frontier["expected_return"]
            expected_volatility = frontier["expected_volatility"]
            engine_run = {
                "engine_name": "investment_solution_agent_v2",
                "engine_version": "v0",
                "status": "ready",
                "engine_policy": _asset_allocation_model_policy_label(self.asset_allocation_client),
                "engine_source": "deterministic_v2_frontier",
                "inputs": {
                    "money_pool_id": pool.get("id"),
                    "risk_tolerance": risk,
                    "horizon_years": horizon,
                    "amount": pool.get("amount"),
                },
                "outputs": {
                    "expected_return": expected_return,
                    "expected_volatility": expected_volatility,
                    "security_count": len(securities),
                },
            }
        total_amount = _number(pool.get("amount")) or 0.0
        securities = _normalize_recommended_securities(securities, total_amount=total_amount)
        proposal_id = _proposal_id(pool=pool, objective=objective, assessment_ref=assessment_ref)
        policy_version = _policy_version_for(objective=objective)
        policy_status = "proposed" if operation == "propose_new_policy" else "under_review"
        payload = {
            "id": proposal_id,
            "schema_version": POLICY_SCHEMA_VERSION,
            "version": policy_version,
            "policy_version": policy_version,
            "version_key": f"{proposal_id}:v{policy_version}",
            "client_id": client_id,
            "status": "drafted" if operation == "propose_new_policy" else "updated",
            "artifact_status": "ready",
            "artifact_type": "investment_policy_proposal",
            "policy_operation": operation,
            "source": self.source,
            "objective": objective,
            "source_assessment": assessment_ref,
            "investment_consultation_id": assessment_ref.get("investment_consultation_id"),
            "money_pool": {
                "id": pool.get("id"),
                "label": pool.get("label") or "Money pool",
                "purpose_type": pool.get("purpose_type"),
                "amount": pool.get("amount"),
                "horizon_years": horizon,
                "risk_tolerance": risk,
            },
            "policy": {
                "title": _policy_title(pool),
                "version": policy_version,
                "policy_version": policy_version,
                "status": policy_status,
                "review_status": "pending",
                "capital_required": pool.get("amount"),
                "scope_of_purpose": _scope_of_purpose(pool),
                "risk_profile": risk,
                "horizon_years": horizon,
                "expected_return": expected_return,
                "expected_volatility": expected_volatility,
                "source_investment_consultation_id": assessment_ref.get("investment_consultation_id"),
                "source_assessment_id": assessment_ref.get("assessment_id"),
                "source_assessment_version": assessment_ref.get("assessment_version"),
                "target_allocation": allocation,
                "recommended_securities": securities,
                "risk_management_policy": _risk_management_policy(pool, assessment_ref),
            },
            "portfolio_analytics": {
                "expected_return": expected_return,
                "expected_volatility": expected_volatility,
                "source": engine_source,
            },
            "engine_run": engine_run,
            "calculation_policy": "Main Agent did not calculate; Investment Solution Agent returned this structured proposal.",
            "missing_data": missing_inputs,
        }
        payload["mobile_section_5b"] = build_mobile_section_5b(payload)
        return SubAgentArtifact(
            artifact_type="investment_solution_policy",
            payload=payload,
            writeback_target="client_file.policies",
        )

    def build_exit_artifact(
        self,
        *,
        client_id: str,
        objective: Dict[str, Any],
        client_file: Dict[str, Any],
    ) -> SubAgentArtifact:
        target = _select_policy_target(client_file=client_file, objective=objective)
        payload = {
            "client_id": client_id,
            "artifact_type": "policy_exit",
            "artifact_status": "ready",
            "policy_operation": "policy_exit",
            "source": self.source,
            "objective": objective,
            "policy": {
                "id": target.get("id"),
                "label": target.get("label") or target.get("title"),
                "status": "closed",
                "review_status": "exit_pending_settlement",
            },
            "settlement": {
                "status": "pending",
                "requires_deterministic_service": True,
            },
            "calculation_policy": "Policy exit is a deterministic service handoff, not an LLM action.",
        }
        return SubAgentArtifact(
            artifact_type="investment_solution_policy_exit",
            payload=payload,
            writeback_target="client_file.policies",
        )


def _select_money_pool(*, client_file: Dict[str, Any], objective: Dict[str, Any]) -> Dict[str, Any]:
    objective_pool = objective.get("money_pool")
    if isinstance(objective_pool, dict) and objective_pool:
        pool = dict(objective_pool)
        pool.setdefault("id", objective.get("subject_id") or "pool-current-turn")
        pool.setdefault("label", objective.get("subject") or _policy_title(pool).replace(" IPS Proposal", ""))
        return pool
    investment_request = objective.get("investment_request")
    if isinstance(investment_request, dict) and investment_request:
        return {
            "id": objective.get("subject_id") or "pool-current-turn",
            "label": investment_request.get("pool_label") or objective.get("subject") or "Money pool",
            "purpose_type": investment_request.get("purpose") or investment_request.get("purpose_type") or "growth",
            "amount": investment_request.get("amount"),
            "horizon_years": investment_request.get("horizon_years"),
            "risk_tolerance": investment_request.get("risk"),
            "funding_source": investment_request.get("funding_source"),
        }
    pools = client_file.get("money_pools") if isinstance(client_file, dict) else []
    pools = [item for item in pools if isinstance(item, dict)] if isinstance(pools, list) else []
    if not pools:
        return {
            "id": objective.get("subject_id") or "pool-unknown",
            "label": objective.get("subject") or "Money pool",
            "purpose_type": "growth",
        }
    target = _objective_subject_id(objective)
    if target:
        for pool in pools:
            if str(pool.get("id") or "") == target:
                return pool
    return pools[0]


def _select_signed_assessment(
    *,
    client_file: Dict[str, Any],
    objective: Dict[str, Any],
    require_requested_id: bool = True,
) -> Optional[Dict[str, Any]]:
    requested_assessment_id = _objective_assessment_id(objective)
    requested_pool_id = _objective_subject_id(objective) or str(objective.get("money_pool_id") or "")
    direct = _direct_assessment_from_objective(objective)
    if direct and _assessment_is_signed(direct):
        if _assessment_matches(direct, assessment_id=requested_assessment_id, money_pool_id=requested_pool_id):
            return direct
    if require_requested_id and not requested_assessment_id:
        return None

    candidates = [
        candidate
        for candidate in _assessment_candidates(client_file)
        if _assessment_is_signed(candidate)
        and _assessment_matches(candidate, assessment_id=requested_assessment_id, money_pool_id=requested_pool_id)
    ]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (
            _assessment_version(item),
            str(item.get("signed_off_at") or item.get("updated_at") or item.get("created_at") or ""),
        ),
        reverse=True,
    )[0]


def _direct_assessment_from_objective(objective: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for key in ("signed_assessment", "investment_assessment", "assessment"):
        value = objective.get(key)
        if isinstance(value, dict) and value:
            assessment = dict(value)
            assessment.setdefault("assessment_id", _objective_assessment_id(objective))
            assessment.setdefault("money_pool_id", objective.get("money_pool_id") or objective.get("subject_id"))
            assessment.setdefault("signed_off_at", objective.get("signed_off_at"))
            return assessment
    return None


def _assessment_candidates(client_file: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(client_file, dict):
        return []
    candidates: List[Dict[str, Any]] = []
    for key in (
        "investment_assessments",
        "signed_investment_assessments",
        "assessments",
        "financial_planning_assessments",
    ):
        candidates.extend(_assessment_rows(client_file.get(key)))
    artifacts = client_file.get("artifacts")
    if isinstance(artifacts, dict):
        candidates.extend(_assessment_rows(artifacts.get("plans")))
        candidates.extend(_assessment_rows(artifacts.get("assessments")))
    plans = client_file.get("plans")
    if isinstance(plans, dict):
        candidates.extend(_assessment_rows(plans.get("writebacks")))
        candidates.extend(_assessment_rows(plans.get("artifacts")))
    elif isinstance(plans, list):
        candidates.extend(_dict_rows(plans))

    recent_writebacks = client_file.get("recent_writebacks")
    if isinstance(recent_writebacks, list):
        for writeback in recent_writebacks:
            if not isinstance(writeback, dict):
                continue
            if str(writeback.get("operation") or "") != "record_assessment_signoff":
                continue
            values = writeback.get("values") if isinstance(writeback.get("values"), dict) else {}
            if values:
                candidates.append(values)

    normalized: List[Dict[str, Any]] = []
    for row in candidates:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
        if _is_investment_assessment_payload(payload):
            normalized.append(payload)
    return normalized


def _assessment_rows(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, dict) and _is_investment_assessment_payload(value):
        return [value]
    return _dict_rows(value)


def _dict_rows(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        rows = value.get("items") if isinstance(value.get("items"), list) else None
        if rows is not None:
            return [item for item in rows if isinstance(item, dict)]
        return [item for item in value.values() if isinstance(item, dict)]
    return []


def _is_investment_assessment_payload(payload: Dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("schema_version") == ASSESSMENT_SCHEMA_VERSION:
        return True
    if payload.get("analysis_type") == "internal_investment_assessment":
        return True
    if payload.get("artifact_type") == "investment_assessment":
        return True
    return bool(payload.get("assessment_id") and (payload.get("assessment") or payload.get("sign_off_summary")))


def _assessment_matches(
    assessment: Dict[str, Any],
    *,
    assessment_id: str,
    money_pool_id: str,
) -> bool:
    if assessment_id and _assessment_id(assessment) != assessment_id:
        return False
    if money_pool_id and _assessment_money_pool_id(assessment) != money_pool_id:
        return False
    return True


def _assessment_is_signed(assessment: Dict[str, Any]) -> bool:
    status = str(assessment.get("status") or assessment.get("assessment_status") or "").strip().lower()
    signoff = assessment.get("signoff") if isinstance(assessment.get("signoff"), dict) else {}
    if status in SIGNED_ASSESSMENT_STATUSES:
        return True
    if assessment.get("signed_off_at") and assessment.get("assessment_id"):
        return True
    return signoff.get("signed_off") is True


def _money_pool_from_assessment(
    assessment: Dict[str, Any],
    *,
    client_file: Dict[str, Any],
    objective: Dict[str, Any],
) -> Dict[str, Any]:
    content = _assessment_content(assessment)
    pool_id = _assessment_money_pool_id(assessment) or _objective_subject_id(objective)
    existing_pool = _find_money_pool(client_file=client_file, pool_id=pool_id)
    label = (
        assessment.get("pool_label")
        or content.get("pool_label")
        or (existing_pool or {}).get("label")
        or objective.get("subject")
        or "Money pool"
    )
    return {
        **(existing_pool or {}),
        "id": pool_id or (existing_pool or {}).get("id") or "pool-current-assessment",
        "label": label,
        "purpose_type": content.get("purpose") or content.get("purpose_type") or (existing_pool or {}).get("purpose_type"),
        "amount": _first_present(
            content.get("amount"),
            content.get("investment_amount"),
            content.get("capital_required"),
            (existing_pool or {}).get("amount"),
        ),
        "horizon_years": _first_present(
            content.get("horizon_years"),
            content.get("horizon"),
            (existing_pool or {}).get("horizon_years"),
        ),
        "risk_tolerance": _first_present(
            content.get("target_risk"),
            content.get("recommended_risk"),
            content.get("risk"),
            content.get("risk_tolerance"),
            (existing_pool or {}).get("risk_tolerance"),
        ),
        "target_volatility_pct": _first_present(
            content.get("target_volatility_pct"),
            content.get("target_volatility"),
            (existing_pool or {}).get("target_volatility_pct"),
        ),
        "funding_source": content.get("funding_source") or (existing_pool or {}).get("funding_source"),
        "excluded_asset_classes": content.get("exclusions") or content.get("excluded_asset_classes") or [],
        "complexity_preference": content.get("complexity_preference"),
    }


def _assessment_ref(assessment: Dict[str, Any], *, objective: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": ASSESSMENT_SCHEMA_VERSION,
        "investment_consultation_id": _assessment_investment_consultation_id(assessment)
        or str(objective.get("investment_consultation_id") or ""),
        "assessment_id": _assessment_id(assessment) or _objective_assessment_id(objective),
        "assessment_version": _assessment_version(assessment),
        "money_pool_id": _assessment_money_pool_id(assessment) or _objective_subject_id(objective),
        "status": "signed_off",
        "signed_off_at": assessment.get("signed_off_at") or objective.get("signed_off_at"),
    }


def _assessment_content(assessment: Dict[str, Any]) -> Dict[str, Any]:
    consultation_basis = assessment.get("consultation_basis")
    if isinstance(consultation_basis, dict) and consultation_basis:
        return consultation_basis
    basis = assessment.get("basis")
    if isinstance(basis, dict) and basis:
        return basis
    content = assessment.get("assessment") if isinstance(assessment.get("assessment"), dict) else {}
    if content:
        basis = content.get("basis") if isinstance(content.get("basis"), dict) else {}
        return basis or content
    request = assessment.get("request") if isinstance(assessment.get("request"), dict) else {}
    signoff = assessment.get("sign_off_summary") if isinstance(assessment.get("sign_off_summary"), dict) else {}
    return {**signoff, **request}


def _assessment_id(assessment: Dict[str, Any]) -> str:
    return str(assessment.get("assessment_id") or assessment.get("id") or "").strip()


def _assessment_money_pool_id(assessment: Dict[str, Any]) -> str:
    content = _assessment_content(assessment)
    return str(
        assessment.get("money_pool_id")
        or content.get("money_pool_id")
        or content.get("pool_id")
        or ""
    ).strip()


def _assessment_investment_consultation_id(assessment: Dict[str, Any]) -> str:
    content = _assessment_content(assessment)
    return str(
        assessment.get("investment_consultation_id")
        or content.get("investment_consultation_id")
        or ""
    ).strip()


def _assessment_version(assessment: Dict[str, Any]) -> int:
    for key in ("assessment_version", "version"):
        value = assessment.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return 1


def _find_money_pool(*, client_file: Dict[str, Any], pool_id: str) -> Optional[Dict[str, Any]]:
    pools = client_file.get("money_pools") if isinstance(client_file, dict) else []
    for pool in _dict_rows(pools):
        if str(pool.get("id") or "") == str(pool_id or ""):
            return pool
    return None


def _objective_subject_id(objective: Dict[str, Any]) -> Optional[str]:
    for key in ("subject_id", "money_pool_id", "source_id"):
        value = objective.get(key)
        if value:
            return str(value)
    target = objective.get("target_writeback")
    if isinstance(target, dict) and target.get("subject_id"):
        return str(target["subject_id"])
    return None


def _objective_assessment_id(objective: Dict[str, Any]) -> str:
    for key in ("assessment_id", "source_assessment_id"):
        value = objective.get(key)
        if value:
            return str(value)
    return ""


def _risk_from_pool(pool: Dict[str, Any]) -> str:
    risk = str(pool.get("risk_tolerance") or pool.get("risk") or "").lower()
    if "conservative" in risk:
        return "conservative"
    if "aggressive" in risk:
        return "aggressive"
    if "moderate growth" in risk:
        return "moderate_growth"
    if "moderate" in risk:
        return "moderate"
    return "moderate"


def _horizon_from_pool(pool: Dict[str, Any]) -> Optional[int]:
    for key in ("horizon_years", "horizon"):
        value = pool.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
    text = str(pool.get("horizon_text") or "")
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def _frontier_for(*, risk: str, horizon_years: Optional[int]) -> Dict[str, float]:
    base = {
        "conservative": (0.048, 0.075),
        "moderate": (0.064, 0.118),
        "moderate_growth": (0.071, 0.142),
        "aggressive": (0.082, 0.18),
    }.get(risk, (0.064, 0.118))
    expected_return, expected_volatility = base
    if horizon_years is not None and horizon_years < 7:
        expected_volatility = max(0.055, expected_volatility - 0.018)
    return {
        "expected_return": round(expected_return, 4),
        "expected_volatility": round(expected_volatility, 4),
    }


def _allocation_for(*, risk: str, purpose: str) -> Dict[str, float]:
    if risk == "conservative":
        return {"VTI": 0.35, "VXUS": 0.1, "BND": 0.45, "SGOV": 0.1}
    if risk == "moderate_growth":
        return {"VTI": 0.58, "VXUS": 0.18, "BND": 0.18, "SGOV": 0.06}
    if risk == "aggressive":
        return {"VTI": 0.68, "VXUS": 0.22, "BND": 0.08, "SGOV": 0.02}
    if purpose == "education":
        return {"VTI": 0.48, "VXUS": 0.12, "BND": 0.32, "SGOV": 0.08}
    return {"VTI": 0.52, "VXUS": 0.16, "BND": 0.26, "SGOV": 0.06}


def _securities_from_allocation(allocation: Dict[str, float], *, total_amount: Optional[float] = None) -> List[Dict[str, Any]]:
    names = {
        "VTI": "US total stock market",
        "VXUS": "International stock market",
        "BND": "US aggregate bonds",
        "SGOV": "Treasury bills",
    }
    asset_classes = {
        "VTI": "US Equity",
        "VXUS": "International Equity",
        "BND": "US Aggregate Bond",
        "SGOV": "Cash",
    }
    return [
        {
            "symbol": symbol,
            "ticker": symbol,
            "name": names.get(symbol, symbol),
            "security_name": names.get(symbol, symbol),
            "asset_class": asset_classes.get(symbol, "Other"),
            "weight": weight,
            "percentage": round(weight * 100.0, 4),
            "amount": round(float(total_amount or 0.0) * weight, 2) if total_amount else None,
        }
        for symbol, weight in allocation.items()
    ]


def _normalize_recommended_securities(
    securities: List[Dict[str, Any]],
    *,
    total_amount: float,
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for row in securities:
        if not isinstance(row, dict):
            continue
        symbol = str(
            row.get("symbol")
            or row.get("ticker")
            or row.get("recommended_security")
            or row.get("security")
            or ""
        ).strip()
        if not symbol:
            continue
        weight = _number(row.get("weight"))
        if weight is None:
            weight = _number(row.get("percentage"))
            if weight is not None:
                weight = weight / 100.0 if abs(weight) > 1.0 else weight
        weight = float(weight or 0.0)
        amount = _number(row.get("amount") or row.get("notional"))
        if amount is None and total_amount:
            amount = total_amount * weight
        normalized.append({
            **row,
            "recommended_security": row.get("recommended_security") or symbol,
            "symbol": symbol,
            "ticker": row.get("ticker") or symbol,
            "name": row.get("name") or row.get("security_name") or symbol,
            "security_name": row.get("security_name") or row.get("name") or symbol,
            "asset_class": row.get("asset_class") or row.get("asset_class_classification") or "Unclassified",
            "weight": round(weight, 8),
            "percentage": round(weight * 100.0, 4),
            "amount": round(float(amount or 0.0), 2),
        })
    return sorted(normalized, key=lambda item: item.get("weight") or 0.0, reverse=True)


def _policy_title(pool: Dict[str, Any]) -> str:
    label = str(pool.get("label") or pool.get("purpose_type") or "Investment").strip()
    return f"{label} Policy"


def _proposal_id(
    *,
    pool: Dict[str, Any],
    objective: Dict[str, Any],
    assessment_ref: Optional[Dict[str, Any]] = None,
) -> str:
    assessment_id = str((assessment_ref or {}).get("assessment_id") or "").strip()
    if assessment_id:
        return f"proposal-{assessment_id}"
    objective_id = str(objective.get("id") or "")
    if objective_id:
        return f"proposal-{objective_id}"
    pool_id = str(pool.get("id") or "money-pool")
    return f"proposal-{pool_id}"


def _missing_policy_inputs(pool: Dict[str, Any]) -> List[str]:
    missing: List[str] = []
    if pool.get("amount") in (None, "", [], {}):
        missing.append("amount")
    if _horizon_from_pool(pool) is None:
        missing.append("horizon")
    if not pool.get("risk_tolerance") and not pool.get("risk"):
        missing.append("risk_tolerance")
    return missing


def _scope_of_purpose(pool: Dict[str, Any]) -> str:
    purpose = str(pool.get("purpose_type") or pool.get("purpose") or "the signed investment purpose").strip()
    amount = _number(pool.get("amount"))
    horizon = _horizon_from_pool(pool)
    amount_text = f"${amount:,.0f}" if amount is not None else "the signed capital"
    horizon_text = f"over about {horizon} years" if horizon is not None else "over the signed horizon"
    return f"Deploy {amount_text} {horizon_text} for {purpose} within the signed assessment constraints."


def _risk_management_policy(pool: Dict[str, Any], assessment_ref: Optional[Dict[str, Any]] = None) -> List[str]:
    """Carry through signed monitoring / risk rules from upstream context.

    These are business facts or model/agent outputs, not UI defaults. If the
    consultation/pool has not produced rules yet, return an empty list and the
    mobile 5b UI will omit section 09.
    """

    candidates = (
        pool.get("risk_management_policy"),
        pool.get("riskManagementPolicy"),
        pool.get("monitoring_rules"),
        pool.get("monitoringRules"),
        (assessment_ref or {}).get("risk_management_policy"),
        (assessment_ref or {}).get("riskManagementPolicy"),
        (assessment_ref or {}).get("monitoring_rules"),
        (assessment_ref or {}).get("monitoringRules"),
    )
    for value in candidates:
        rules = _string_list(value)
        if rules:
            return rules
    guardrails = pool.get("guardrails") or (assessment_ref or {}).get("guardrails")
    if isinstance(guardrails, dict):
        return [str(value).strip() for value in guardrails.values() if str(value).strip()]
    return []


def _string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [line.strip(" -•\t") for line in value.splitlines() if line.strip(" -•\t")]
    return []


def _policy_version_for(*, objective: Dict[str, Any]) -> int:
    for key in ("policy_version", "version"):
        value = objective.get(key)
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit() and int(value) > 0:
            return int(value)
    return 1


def _policy_operation_from_objective(objective: Dict[str, Any]) -> str:
    explicit = str(objective.get("operation") or "").strip().lower()
    if explicit in {"propose_new_policy", "policy_drafting", "draft_policy"}:
        return "propose_new_policy"
    if explicit in {"policy_update", "update_policy", "revise_policy"}:
        return "policy_update"
    if explicit in {"policy_exit", "exit_policy", "close_policy"}:
        return "policy_exit"
    text = " ".join(str(objective.get(key) or "") for key in ("id", "objective", "ask")).lower()
    if "exit" in text or "close policy" in text:
        return "policy_exit"
    if "update" in text or "revise" in text or "stale" in text:
        return "policy_update"
    return "propose_new_policy"


def _select_policy_target(*, client_file: Dict[str, Any], objective: Dict[str, Any]) -> Dict[str, Any]:
    policies = client_file.get("policies") if isinstance(client_file.get("policies"), dict) else {}
    active = policies.get("active") if isinstance(policies.get("active"), list) else []
    proposed = policies.get("proposed") if isinstance(policies.get("proposed"), list) else []
    candidates = [item for item in [*active, *proposed] if isinstance(item, dict)]
    target_id = _objective_subject_id(objective)
    if target_id:
        for item in candidates:
            if str(item.get("id") or "") == target_id:
                return item
    return candidates[0] if candidates else {"id": target_id or "policy-unknown"}


def _asset_allocation_model_policy_label(asset_allocation_client: Optional["AssetAllocationModelClient"]) -> str:
    try:
        from advisor.tools.deterministic_tools.execution import engine_policy

        policy = engine_policy()
    except Exception:  # pragma: no cover - import safety
        policy = "graceful"
    if asset_allocation_client is None or not asset_allocation_client.enabled:
        return f"{policy}:asset_allocation_model_disabled"
    return f"{policy}:asset_allocation_model_enabled"


def _blocked_policy_artifact(
    *,
    client_id: str,
    objective: Dict[str, Any],
    operation: str,
    source: str,
    status: str,
    reason: str,
    missing_data: List[str],
    assessment_ref: Optional[Dict[str, Any]] = None,
) -> SubAgentArtifact:
    return SubAgentArtifact(
        artifact_type="investment_solution_policy",
        payload={
            "schema_version": POLICY_SCHEMA_VERSION,
            "client_id": client_id,
            "status": status,
            "artifact_status": "blocked",
            "artifact_type": "investment_policy_proposal",
            "policy_operation": operation,
            "source": source,
            "objective": objective,
            "source_assessment": assessment_ref or {},
            "missing_data": missing_data,
            "calculation_policy": reason,
        },
        writeback_target="client_file.policies",
    )


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace("$", "").replace(",", "").strip())
        except ValueError:
            return None
    return None

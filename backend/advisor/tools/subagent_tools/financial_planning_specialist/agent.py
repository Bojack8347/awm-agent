"""Financial Planning sub-agent for AWM agent v2.

This module is deliberately deterministic. The Main Agent can ask for wealth
data or dispatch a planning objective, but calculations and structured numbers
come from this boundary instead of being invented in the conversation layer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from advisor.tools.subagent_tools.common.interfaces import SubAgentArtifact

if TYPE_CHECKING:
    from advisor.tools.deterministic_tools.execution import CashflowEngineClient


FINANCIAL_PLANNING_ARTIFACT_TYPE = "financial_planning_analysis"
INVESTMENT_CONSULTATION_SCHEMA_VERSION = "investment_consultation.v1"
INVESTMENT_ASSESSMENT_SCHEMA_VERSION = "investment_assessment.v1"


@dataclass(frozen=True)
class FinancialPlanningAgentV2:
    """Small deterministic FP specialist behind the v2 sub-agent contract."""

    source: str = "financial_planning_agent_v2"
    cashflow_client: Optional["CashflowEngineClient"] = None

    def run_cashflow_projection(
        self,
        *,
        client_id: str,
        session_id: str,
        question: str,
        scenario: Dict[str, Any],
        client_file: Dict[str, Any],
        mortgage_defaults_authorized: bool = False,
        monte_carlo_paths: Optional[int] = None,
        detail_report_groups: Optional[List[str]] = None,
        authorized_public_model_inputs: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Validate and execute a future-looking cash-flow request.

        The method refuses silent defaults. Mortgage fallback values are allowed
        only when the runtime passes server-derived authorization from the
        original user turn. A not-ready result gives the Main Agent one precise
        next question and preserves the pending scenario.
        """

        from advisor.tools.deterministic_tools.run_cashflow_projection.scenarios import (
            CashflowCapabilityDecision,
            CashflowClientInput,
            cashflow_input_readiness,
        )
        from advisor.tools.deterministic_tools.execution import (
            build_cashflow_payload_from_client_file,
        )

        decision = CashflowCapabilityDecision.from_dict(scenario)
        if decision.validation_errors:
            return {
                "schema_version": "awm.cashflow_result.v2",
                "model": {"name": "cashflow", "operation": "simulate", "model_version": None},
                "request": {
                    "call_id": None,
                    "scenario_label": question[:160],
                    "requested_input": scenario,
                    "effective_input": None,
                },
                "status": {
                    "execution": "not_run",
                    "validation": "invalid_request",
                    "analysis_grade": "not_run",
                    "valid_for_recommendation": False,
                    "warnings": list(decision.validation_errors),
                    "error": "invalid_request",
                    "missing_required_metrics": [],
                    "normalization_errors": [],
                },
                "scenario": {"label": question[:160]},
                "headline": {
                    "conclusion": "invalid_request",
                    "summary": "The cash-flow request did not match the supported scenario contract.",
                },
                "metrics": {},
                "drivers": [],
                "missing_data": list(decision.validation_errors),
                "source": self.source,
                "client_id": client_id,
                "session_id": session_id,
                "calculation_policy": "No model was run for an invalid scenario request.",
            }
        canonical_input = CashflowClientInput.from_client_file(client_file)
        base_payload = build_cashflow_payload_from_client_file(
            client_file,
            client_input=canonical_input,
            mortgage_defaults_authorized=mortgage_defaults_authorized,
        )
        readiness = cashflow_input_readiness(
            client_file,
            decision=decision,
            client_input=canonical_input,
            engine_payload=base_payload,
            monte_carlo_paths=monte_carlo_paths,
        )
        if not readiness["ready"]:
            return {
                "schema_version": "awm.cashflow_result.v2",
                "model": {"name": "cashflow", "operation": "simulate", "model_version": None},
                "request": {
                    "call_id": None,
                    "scenario_label": question[:160],
                    "requested_input": {
                        "requested_metrics": decision.requested_metrics,
                        "scenario_changes": decision.scenario_changes,
                    },
                    "effective_input": None,
                },
                "status": {
                    "execution": "not_run",
                    "validation": "missing_data",
                    "analysis_grade": "not_run",
                    "valid_for_recommendation": False,
                    "warnings": [],
                    "error": "missing_required_inputs",
                    "missing_required_metrics": [],
                    "normalization_errors": [],
                },
                "scenario": {"label": question[:160]},
                "headline": {
                    "conclusion": "more_information_required",
                    "summary": "Required Client File inputs are missing for this scenario.",
                },
                "metrics": {},
                "drivers": [],
                "missing_data": readiness["missing_required_inputs"],
                "next_question": readiness["next_question"],
                "source": self.source,
                "client_id": client_id,
                "session_id": session_id,
                "calculation_policy": "No model was run and no missing input was silently defaulted.",
            }

        if self.cashflow_client is None:
            return {
                "schema_version": "awm.cashflow_result.v2",
                "model": {"name": "cashflow", "operation": "simulate", "model_version": None},
                "request": {
                    "call_id": None,
                    "scenario_label": question[:160],
                    "requested_input": scenario,
                    "effective_input": None,
                },
                "status": {
                    "execution": "unavailable",
                    "validation": "failed",
                    "analysis_grade": "not_run",
                    "valid_for_recommendation": False,
                    "warnings": ["Cash-flow engine is not configured for the v2 Financial Planning boundary."],
                    "error": "engine_unavailable",
                    "missing_required_metrics": [],
                    "normalization_errors": [],
                },
                "scenario": {"label": question[:160]},
                "headline": {
                    "conclusion": "analysis_unavailable",
                    "summary": "The requested cash-flow scenario is ready but the model is unavailable.",
                },
                "metrics": {},
                "drivers": [],
                "missing_data": [],
                "source": self.source,
                "client_id": client_id,
                "session_id": session_id,
                "calculation_policy": "No deterministic projection fallback was substituted for the unavailable engine.",
            }

        result = self.cashflow_client.analyze_scenario(
            client_file=client_file,
            scenario=decision.to_dict(),
            question=question,
            mortgage_defaults_authorized=mortgage_defaults_authorized,
            monte_carlo_paths=monte_carlo_paths,
            detail_report_groups=detail_report_groups,
            authorized_public_model_inputs=authorized_public_model_inputs,
        )
        result["source"] = self.source
        result["client_id"] = client_id
        result["session_id"] = session_id
        return result

    def build_artifact(
        self,
        *,
        client_id: str,
        objective: Dict[str, Any],
        client_file: Dict[str, Any],
    ) -> SubAgentArtifact:
        facts = _safe_dict(client_file.get("facts")) if isinstance(client_file, dict) else {}
        summary = _safe_dict(client_file.get("summary")) if isinstance(client_file, dict) else {}
        structured = _safe_dict(client_file.get("structured_facts")) if isinstance(client_file, dict) else {}
        from client_file.financial_position import resolve_financial_position

        financial_position = resolve_financial_position(
            client_id=client_id, client_file=client_file,
        )
        operands = financial_position.get("net_worth_operands") or []
        net_worth = sum(
            (float(item.get("value") or 0) * (-1 if item.get("direction") == "subtract" else 1))
            for item in operands if isinstance(item, dict)
        ) if operands and not financial_position.get("conflicts") else None
        employer_value = sum(
            float(item.get("value") or 0)
            for item in financial_position.get("employer_stock_operands") or []
            if isinstance(item, dict)
        )
        concentration = (
            employer_value / net_worth
            if employer_value and net_worth is not None and net_worth > 0
            else None
        )
        findings = [
            item
            for item in _findings(
                facts=facts,
                summary=summary,
                structured=structured,
            )
            if item.get("type") != "net_worth"
        ]
        if net_worth is not None:
            findings.insert(
                0,
                {
                    "type": "net_worth",
                    "value": net_worth,
                    "source": "financial_position",
                    "snapshot_id": financial_position.get("snapshot_id"),
                },
            )
        analysis_type = _analysis_type_from_objective(objective)
        payload = {
            "client_id": client_id,
            "status": "ready",
            "objective": objective,
            "analysis_type": analysis_type,
            "capability": "financial_planning_core",
            "source": self.source,
            "engine": _engine_metadata(self.cashflow_client),
            "engine_policy": _engine_policy_label(self.cashflow_client),
            "calculation_policy": "All numeric fields are read from Client File or deterministic FP helpers.",
            "summary": {
                "net_worth": net_worth,
                "employer_stock_concentration": concentration,
                "financial_position_snapshot_id": financial_position.get("snapshot_id"),
                "financial_position_completeness": financial_position.get("completeness"),
                "money_pool_count": summary.get("money_pool_count"),
                "active_policy_count": summary.get("active_policy_count"),
                "proposed_policy_count": summary.get("proposed_policy_count"),
            },
            "findings": findings,
            "missing_data": _artifact_missing_data(facts=facts, summary=summary, structured=structured),
            "financial_position": financial_position,
        }
        silent_skill = _silent_skill_for_analysis(analysis_type)
        if silent_skill:
            payload["silent_skill"] = silent_skill
        return SubAgentArtifact(
            artifact_type=FINANCIAL_PLANNING_ARTIFACT_TYPE,
            payload=payload,
            writeback_target="client_file.plans",
        )

    def assess_investment_request(
        self,
        *,
        client_id: str,
        session_id: str = "",
        request: Dict[str, Any],
        client_file: Dict[str, Any],
    ) -> SubAgentArtifact:
        """Silent Investment-Assessment: a best-interest check for one pool.

        Phase 5a of CLIENT_JOURNEY_SIMULATION.md. Given the client's investment
        request (amount / horizon / risk / purpose / funding source) this tests it
        against the whole Client File for **alignment** (emergency reserve intact,
        income covers spending, reduces vs. adds a flagged risk) and **internal
        consistency** (amount <-> horizon <-> risk <-> purpose), returning an
        ``aligned`` or ``misaligned`` verdict plus a client-facing sign-off
        summary. Misaligned records the concern; the Main Agent decides whether to
        reopen the discussion. We flag, we never block — the verdict is advisory.
        """
        facts = _safe_dict(client_file.get("facts")) if isinstance(client_file, dict) else {}
        summary = _safe_dict(client_file.get("summary")) if isinstance(client_file, dict) else {}
        structured = _safe_dict(client_file.get("structured_facts")) if isinstance(client_file, dict) else {}
        assessment = _investment_assessment(request, facts=facts, summary=summary, structured=structured)
        assessment_id = str(request.get("assessment_id") or _stable_assessment_id(client_id, request))
        assessment_version = _positive_int(request.get("assessment_version")) or 1
        money_pool_id = str(request.get("money_pool_id") or request.get("pool_id") or "").strip()
        investment_consultation_id = str(
            request.get("investment_consultation_id")
            or request.get("consultation_id")
            or _stable_investment_consultation_id(client_id, request)
        )
        assessment_body = _safe_dict(assessment.get("assessment"))
        consultation_basis = dict(_safe_dict(assessment_body.get("basis")))
        consultation_basis["schema_version"] = "investment_consultation_basis.v1"
        consultation_basis["investment_consultation_id"] = investment_consultation_id
        if money_pool_id:
            consultation_basis["money_pool_id"] = money_pool_id
        assessment_body["basis"] = consultation_basis
        payload = {
            "client_id": client_id,
            "session_id": session_id,
            "status": "pending_client_signoff",
            "artifact_status": "ready",
            "schema_version": INVESTMENT_ASSESSMENT_SCHEMA_VERSION,
            "artifact_type": "investment_assessment",
            "investment_consultation_id": investment_consultation_id,
            "assessment_id": assessment_id,
            "assessment_version": assessment_version,
            "assessment_status": "pending_client_signoff",
            "money_pool_id": money_pool_id,
            "consultation_basis": consultation_basis,
            "analysis_type": "internal_investment_assessment",
            "silent_skill": "internal-investment-assessment",
            "source": self.source,
            "calculation_policy": (
                "Verdict + findings are deterministic (best-interest check against the "
                "Client File); Main Agent narrates and gates on sign-off only."
            ),
            "assessment": assessment_body,
        }
        return SubAgentArtifact(
            artifact_type=FINANCIAL_PLANNING_ARTIFACT_TYPE,
            payload=payload,
            writeback_target="client_file.plans",
        )

def _lookup_known_value(
    *,
    question_type: str,
    facts: Dict[str, Any],
    summary: Dict[str, Any],
    structured: Dict[str, Any],
) -> Any:
    keys_by_type = {
        "net_worth": ("net_worth", "estimated_net_worth"),
        "future_spending": ("future_spending", "projected_spending", "planned_expenses"),
        "cashflow": ("cashflow", "cash_flow", "annual_cashflow"),
        "retirement_readiness": ("retirement_readiness", "retirement_gap"),
        "concentration_risk": ("employer_stock_concentration", "single_stock_concentration"),
    }
    for key in keys_by_type.get(question_type, ()):
        if key in summary:
            return summary[key]
        if key in facts:
            return facts[key]
        if key in structured:
            return structured[key]
    if question_type == "net_worth":
        return _derive_simple_net_worth(facts)
    if question_type == "cashflow":
        return _derive_cashflow_surplus(facts)
    return None


def _derive_simple_net_worth(facts: Dict[str, Any]) -> Optional[float]:
    asset_keys = ("cash", "taxable_brokerage", "retirement_accounts", "college_529", "home_value")
    debt_keys = ("mortgage_balance", "mortgage", "liabilities", "debt", "debts")
    assets = [_number(facts.get(key)) for key in asset_keys]
    debts = [_number(facts.get(key)) for key in debt_keys]
    if not any(value is not None for value in assets + debts):
        return None
    return float(sum(value or 0 for value in assets) - sum(value or 0 for value in debts))


def _derive_cashflow_surplus(facts: Dict[str, Any]) -> Optional[float]:
    income = _first_number(facts, "income", "annual_income", "household_income")
    spending = _first_number(facts, "spending", "annual_spending", "planned_expenses")
    if income is None or spending is None:
        return None
    return float(income - spending)


def _artifact_missing_data(
    *,
    facts: Dict[str, Any],
    summary: Dict[str, Any],
    structured: Dict[str, Any],
) -> List[str]:
    missing: List[str] = []
    if _lookup_known_value(question_type="net_worth", facts=facts, summary=summary, structured=structured) is None:
        missing.append("net_worth_components")
    if not any(key in facts for key in ("annual_income", "household_income", "income_context")) and "income" not in structured:
        missing.append("income")
    if not any(key in facts for key in ("annual_spending", "planned_expenses")) and "future_spending" not in summary:
        missing.append("planned_expenses")
    return missing


def _findings(
    *,
    facts: Dict[str, Any],
    summary: Dict[str, Any],
    structured: Dict[str, Any],
) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    net_worth = _lookup_known_value(question_type="net_worth", facts=facts, summary=summary, structured=structured)
    if net_worth is not None:
        findings.append({
            "type": "net_worth",
            "value": net_worth,
            "source": "client_file",
        })
    concentration = _lookup_known_value(
        question_type="concentration_risk",
        facts=facts,
        summary=summary,
        structured=structured,
    )
    if concentration is not None:
        findings.append({
            "type": "concentration_risk",
            "value": concentration,
            "source": "client_file",
        })
    return findings


def _first_number(facts: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = _number(facts.get(key))
        if value is not None:
            return value
    return None


def _investment_assessment(
    request: Dict[str, Any],
    *,
    facts: Dict[str, Any],
    summary: Dict[str, Any],
    structured: Dict[str, Any],
) -> Dict[str, Any]:
    """Deterministic best-interest verdict for one investment request.

    Returns aligned/misaligned + the alignment findings, internal-consistency
    findings, concerns (each with a severity — ``soft`` never blocks alignment),
    and a client-facing sign-off summary. Checks degrade gracefully: a check is
    skipped, never falsely flagged, when its Client-File input is unknown.
    """
    request = request if isinstance(request, dict) else {}
    amount = _number(request.get("amount"))
    horizon = _number(request.get("horizon_years") or request.get("horizon"))
    risk = str(
        request.get("risk")
        or request.get("risk_tolerance")
        or request.get("risk_preference")
        or ""
    ).strip().lower()
    purpose = str(request.get("purpose") or request.get("purpose_type") or "").strip().lower()
    funding_raw = request.get("funding_source") or request.get("funding")
    target_volatility = _number(request.get("target_volatility_pct") or request.get("target_volatility"))
    liquidity_requirement = (
        request.get("liquidity_requirement")
        or request.get("liquidity_need")
        or request.get("liquidity_preference")
    )
    exclusions = request.get("exclusions") or request.get("excluded_asset_classes") or []
    tax_note = request.get("tax_note") or request.get("tax_tradeoff") or request.get("tax_consideration")

    alignment: List[str] = []
    consistency: List[str] = []
    concerns: List[Dict[str, Any]] = []

    # --- Alignment: does committing this money actually serve the client? ---
    emergency = _first_number(facts, "cash", "emergency_cash", "emergency_reserve")
    if emergency is not None:
        alignment.append(
            f"Client File shows emergency cash of about ${emergency:,.0f} as a separate reserve."
        )

    income = _first_number(facts, "income", "annual_income", "household_income", "income_context")
    spending = _first_number(facts, "spending", "annual_spending", "planned_expenses")
    if income is not None and spending is not None:
        if income > spending:
            alignment.append(
                f"Income (~${income:,.0f}) comfortably covers ~${spending:,.0f} spending, so this money isn't reached for."
            )
        else:
            concerns.append({
                "issue": "cashflow",
                "severity": "hard",
                "detail": "Spending is close to or above income; committing this amount could strain cashflow.",
            })

    concentration = _lookup_known_value(
        question_type="concentration_risk", facts=facts, summary=summary, structured=structured
    )
    if concentration is not None and any(k in purpose for k in ("diversif", "reduce", "growth")):
        alignment.append("It reduces a real risk we flagged (single-stock concentration) rather than adding one.")

    # --- Internal consistency: amount <-> horizon <-> risk <-> purpose ---
    if horizon is not None:
        growthy = any(k in purpose for k in ("growth", "diversif", "retire"))
        if horizon < 3 and risk == "aggressive":
            concerns.append({
                "issue": "risk_horizon",
                "severity": "hard",
                "detail": f"Aggressive risk on a short ~{horizon:.0f}-year horizon can force selling in a downturn.",
            })
        elif horizon >= 10 and risk == "conservative" and growthy:
            concerns.append({
                "issue": "risk_horizon",
                "severity": "soft",
                "detail": f"A {horizon:.0f}-year growth horizon can usually carry more than conservative risk; conservative may leave return on the table.",
            })
        if horizon >= 5 and risk in {"moderate", "aggressive"} and growthy:
            consistency.append(
                f"A {horizon:.0f}-year horizon fits money not needed soon and matches a {risk} growth posture."
            )

    hard_concerns = [c for c in concerns if c.get("severity", "hard") == "hard"]
    verdict = "aligned" if not hard_concerns else "misaligned"
    amount_text = f"${amount:,.0f}" if amount is not None else "the agreed amount"
    horizon_text = f"about {horizon:g} years" if horizon is not None else "the agreed horizon"
    source_text = str(funding_raw or "the agreed funding source").strip()
    purpose_text = str(request.get("purpose") or request.get("purpose_type") or "the agreed purpose").strip()
    first_paragraph = (
        f"You are signing off that this pool is {amount_text} from {source_text}, "
        f"with the purpose of {purpose_text} over {horizon_text}."
    )
    second_parts: List[str] = []
    if risk:
        volatility = None
        if target_volatility is not None:
            volatility_pct = target_volatility * 100 if 0 < target_volatility <= 1 else target_volatility
            volatility = f", targeting about {volatility_pct:g}% volatility"
        second_parts.append(f"The agreed posture is {risk} risk{volatility or ''}.")
    requirements: List[str] = []
    if liquidity_requirement:
        requirements.append(str(liquidity_requirement))
    if exclusions:
        exclusion_items = (
            [item.strip() for item in str(exclusions).split(",") if item.strip()]
            if isinstance(exclusions, str)
            else [str(item).strip() for item in exclusions if str(item).strip()]
        )
        if exclusion_items:
            if len(exclusion_items) == 1:
                exclusion_text = exclusion_items[0]
            elif len(exclusion_items) == 2:
                exclusion_text = f"{exclusion_items[0]} and {exclusion_items[1]}"
            else:
                exclusion_text = ", ".join(exclusion_items[:-1]) + f", and {exclusion_items[-1]}"
            requirements.append("avoid " + exclusion_text)
    elif str(request.get("complexity_preference") or "").strip().lower() in {
        "plain_vanilla",
        "plain vanilla",
        "simple",
        "low_complexity",
    }:
        requirements.append("use plain-vanilla investments only")
    if tax_note:
        requirements.append(str(tax_note))
    if requirements:
        if len(requirements) == 1:
            requirements_text = requirements[0]
        elif len(requirements) == 2:
            requirements_text = f"{requirements[0]} and {requirements[1]}"
        else:
            requirements_text = ", ".join(requirements[:-1]) + f", and {requirements[-1]}"
        second_parts.append("The solution should " + requirements_text + ".")

    return {
        "assessment": {
            "schema_version": INVESTMENT_ASSESSMENT_SCHEMA_VERSION,
            "verdict": verdict,
            "recommended_risk_level": risk or None,
            "severity": "high" if hard_concerns else None,
            "basis": {
                "money_pool_id": request.get("money_pool_id") or request.get("pool_id"),
                "pool_label": request.get("pool_label"),
                "amount": amount,
                "funding_source": funding_raw,
                "purpose": request.get("purpose") or request.get("purpose_type"),
                "horizon_years": horizon,
                "risk": risk or None,
                "target_risk": risk or None,
                "target_volatility_pct": target_volatility,
                "liquidity_requirement": liquidity_requirement,
                "exclusions": exclusions,
                "complexity_preference": request.get("complexity_preference"),
                "tax_note": tax_note,
            },
            "client_summary": {
                "title": "Investment Consultation Summary",
                "subtitle": "For your sign-off",
                "paragraphs": [first_paragraph, " ".join(second_parts)] if second_parts else [first_paragraph],
            },
            "internal_review": {
                "alignment_reasons": alignment,
                "consistency_checks": consistency,
                "concerns": concerns,
            },
        }
    }


def _stable_assessment_id(client_id: str, request: Dict[str, Any]) -> str:
    canonical = json.dumps(
        {"client_id": client_id, "request": request},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"assess-{digest}"


def _stable_investment_consultation_id(client_id: str, request: Dict[str, Any]) -> str:
    canonical = json.dumps(
        {
            "client_id": client_id,
            "money_pool_id": request.get("money_pool_id") or request.get("pool_id"),
            "pool_label": request.get("pool_label"),
            "purpose": request.get("purpose") or request.get("purpose_type"),
            "amount": request.get("amount"),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"consult-{digest}"


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _analysis_type_from_objective(objective: Dict[str, Any]) -> str:
    text = " ".join(str(objective.get(key) or "") for key in ("id", "objective", "ask")).lower()
    if "diagnosis" in text or "diagnoses" in text:
        return "diagnosis"
    if "assessment revalidation" in text or "revalidation" in text:
        return "assessment_revalidation"
    if "internal investment assessment" in text or "investment assessment" in text:
        return "internal_investment_assessment"
    if "assessment" in text:
        return "internal_investment_assessment"
    if "cashflow" in text or "cash flow" in text or "projection" in text:
        return "cashflow_projection"
    if "net worth" in text or "balance" in text or "known value" in text:
        return "snapshot_lookup"
    if "afford" in text or "sizing" in text or "how much" in text:
        return "affordability_sizing"
    if "goal" in text or "education" in text or "retirement" in text or "feasible" in text:
        return "goal_feasibility"
    if "risk capacity" in text or "stress" in text or "downside" in text:
        return "risk_capacity"
    if "changed" in text or "monitor" in text or "material" in text:
        return "monitoring_delta"
    return "other"


def _silent_skill_for_analysis(analysis_type: str) -> Optional[str]:
    mapping = {
        "diagnosis": "diagnosis",
        "internal_investment_assessment": "internal-investment-assessment",
        "assessment_revalidation": "assessment-revalidation",
    }
    return mapping.get(analysis_type)


def _engine_policy_label(cashflow_client: Optional["CashflowEngineClient"]) -> str:
    try:
        from advisor.tools.deterministic_tools.execution import engine_policy

        policy = engine_policy()
    except Exception:  # pragma: no cover - import safety
        policy = "graceful"
    if cashflow_client is None or not cashflow_client.enabled:
        return f"{policy}:cashflow_disabled"
    return f"{policy}:cashflow_enabled"


def _safe_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    return None


@dataclass(frozen=True)
class AssessmentRevalidationAgentV2:
    """Deterministic assessment-revalidation boundary."""

    source: str = "assessment_revalidation_agent_v2"
    cashflow_client: Optional["CashflowEngineClient"] = None

    def evaluate_materiality(
        self,
        *,
        client_file: Dict[str, Any],
        change_hint: str = "",
    ) -> Dict[str, Any]:
        if self.cashflow_client is not None and self.cashflow_client.enabled:
            result = self.cashflow_client.evaluate_materiality(
                client_file=client_file,
                change_hint=change_hint,
            )
            signal = str(result.get("signal") or "")
            if signal.startswith("engine_required"):
                return result
            if result.get("signal") != "engine_unavailable":
                return result
        facts = _safe_dict(client_file.get("facts"))
        structured = _safe_dict(client_file.get("structured_facts"))
        stale_impacts = client_file.get("stale_impacts")
        stale_impacts = stale_impacts if isinstance(stale_impacts, list) else []
        hint = change_hint.lower()

        if facts.get("income_change_material") is True or "income" in hint and any(
            token in hint for token in ("changed", "increase", "raise", "bonus", "rsu", "vest")
        ):
            return {
                "material": True,
                "signal": "income_band_crossed",
                "reason": "Aggregate income changed enough to cross a planning band.",
                "source": self.source,
            }

        if stale_impacts:
            return {
                "material": True,
                "signal": "stale_policy_impact",
                "reason": "Client File reports stale policy or proposal impacts.",
                "impacted_count": len(stale_impacts),
                "source": self.source,
            }

        income = _number(facts.get("household_income")) or _number(structured.get("income"))
        if income is not None and income >= 500_000 and "aggressive" in hint:
            return {
                "material": True,
                "signal": "capacity_preference_tension",
                "reason": "Preference shift may no longer match prior capacity assumptions.",
                "source": self.source,
            }

        return {
            "material": False,
            "signal": "none",
            "reason": "Change is not material under current deterministic rules.",
            "source": self.source,
        }

    def run(
        self,
        *,
        client_id: str,
        objective: Dict[str, Any],
        client_file: Dict[str, Any],
        change_hint: str = "",
    ) -> SubAgentArtifact:
        materiality = self.evaluate_materiality(client_file=client_file, change_hint=change_hint)
        verdict = self._verdict_from_materiality(materiality, client_file=client_file, change_hint=change_hint)
        payload = {
            "client_id": client_id,
            "analysis_type": "assessment_revalidation",
            "silent_skill": "assessment-revalidation",
            "status": "ready",
            "source": self.source,
            "objective": objective,
            "materiality": materiality,
            "verdict": verdict,
            "recommended_actions": self._recommended_actions(verdict),
            "calculation_policy": "Verdict is deterministic; Main Agent narrates only.",
        }
        return SubAgentArtifact(
            artifact_type=FINANCIAL_PLANNING_ARTIFACT_TYPE,
            payload=payload,
            writeback_target="client_file.plans",
        )

    def _verdict_from_materiality(
        self,
        materiality: Dict[str, Any],
        *,
        client_file: Dict[str, Any],
        change_hint: str,
    ) -> str:
        if not materiality.get("material"):
            return "valid"

        signal = str(materiality.get("signal") or "")
        hint = change_hint.lower()
        if signal == "capacity_preference_tension" or any(
            token in hint for token in ("more aggressive", "risk appetite", "preference")
        ):
            return "re_engage"
        if signal in {"income_band_crossed", "stale_policy_impact"}:
            policies = client_file.get("policies")
            if isinstance(policies, list) and policies:
                return "capacity_shift_only"
        return "re_engage"

    def _recommended_actions(self, verdict: str) -> List[str]:
        if verdict == "valid":
            return ["no_action"]
        if verdict == "capacity_shift_only":
            return ["policy_update", "revise_proposal"]
        return ["reopen_investment_consult", "policy_update_if_needed"]


def _engine_metadata(cashflow_client: Optional["CashflowEngineClient"]) -> Dict[str, Any]:
    if cashflow_client is None:
        return {"name": "deterministic_v2", "enabled": False}
    return {
        "name": "cashflow",
        "enabled": bool(cashflow_client.enabled),
    }

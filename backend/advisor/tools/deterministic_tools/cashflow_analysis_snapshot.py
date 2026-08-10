"""Compact, deterministic agent view over a normalized cash-flow result."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional


AGENT_VIEW_SCHEMA_VERSION = "awm.cashflow_agent_view.v1"
SNAPSHOT_SCHEMA_VERSION = "awm.cashflow_analysis_snapshot.v1"


def canonical_fingerprint(value: Any) -> str:
    """Return a stable SHA-256 fingerprint for JSON-compatible model input."""

    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_cashflow_analysis_snapshot(
    *,
    client_id: str,
    session_id: str,
    analysis: Mapping[str, Any],
    recommendation_evidence: Mapping[str, Any],
    client_file_fingerprint: str,
    source_allocation: Optional[Mapping[str, Any]] = None,
    source_allocations: Optional[list[Mapping[str, Any]]] = None,
    projection_source: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build an immutable result snapshot and a concise LLM-facing semantic view."""

    analysis_copy = _json_copy(analysis)
    evidence_copy = _json_copy(recommendation_evidence)
    request = analysis_copy.get("request") if isinstance(analysis_copy.get("request"), dict) else {}
    identity_payload = {
        "client_id": client_id,
        "session_id": session_id,
        "call_id": request.get("call_id"),
        "effective_input": request.get("effective_input"),
        "metrics": analysis_copy.get("metrics"),
    }
    analysis_id = f"cashflow_{canonical_fingerprint(identity_payload)[:24]}"
    created_at = datetime.now(timezone.utc).isoformat()
    normalized_source_allocations = [
        _json_copy(item)
        for item in source_allocations or []
        if isinstance(item, Mapping)
    ]
    if not normalized_source_allocations and isinstance(source_allocation, Mapping):
        normalized_source_allocations = [_json_copy(source_allocation)]
    agent_view = build_cashflow_agent_view(
        analysis_id=analysis_id,
        analysis=analysis_copy,
        recommendation_evidence=evidence_copy,
        created_at=created_at,
        source_allocation=source_allocation,
        source_allocations=normalized_source_allocations,
    )
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "analysis_id": analysis_id,
        "client_id": client_id,
        "session_id": session_id,
        "created_at": created_at,
        "client_file_fingerprint": client_file_fingerprint,
        "input_fingerprint": canonical_fingerprint(request.get("effective_input")),
        "source_allocation": _json_copy(source_allocation)
        if isinstance(source_allocation, Mapping)
        else None,
        "source_allocations": normalized_source_allocations,
        "analysis": analysis_copy,
        "projection_source": (
            _json_copy(projection_source)
            if isinstance(projection_source, Mapping)
            else None
        ),
        "recommendation_evidence": evidence_copy,
        "cashflow_agent_view": agent_view,
    }


def build_cashflow_agent_view(
    *,
    analysis_id: str,
    analysis: Mapping[str, Any],
    recommendation_evidence: Mapping[str, Any],
    created_at: Optional[str] = None,
    source_allocation: Optional[Mapping[str, Any]] = None,
    source_allocations: Optional[list[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Expose typed claims and precomputed interpretations without new arithmetic."""

    claims: Dict[str, Dict[str, Any]] = {}
    raw_claims = recommendation_evidence.get("claims")
    if isinstance(raw_claims, list):
        for claim in raw_claims:
            if not isinstance(claim, dict):
                continue
            key = str(claim.get("metric_key") or "").strip()
            claim_id = str(claim.get("claim_id") or "").strip()
            if not key or not claim_id:
                continue
            claims[key] = {
                "value": _json_copy(claim.get("value")),
                "unit": claim.get("unit"),
                "claim_id": claim_id,
                "evidence_ref": f"run_cashflow_projection/{claim_id}",
            }

    scenario = analysis.get("scenario") if isinstance(analysis.get("scenario"), dict) else {}
    request = analysis.get("request") if isinstance(analysis.get("request"), dict) else {}
    status = analysis.get("status") if isinstance(analysis.get("status"), dict) else {}
    run_metadata = (
        analysis.get("native_result_metadata")
        if isinstance(analysis.get("native_result_metadata"), dict)
        else {}
    )
    simulation = _cashflow_simulation_summary(claims, run_metadata)
    resolved_assumptions = [
        _json_copy(item)
        for item in analysis.get("resolved_assumptions") or []
        if isinstance(item, dict)
    ]
    configured_defaults = [
        item
        for item in resolved_assumptions
        if str(item.get("reason") or "") == "client_value_not_supplied"
    ]
    effective_parameters = [
        item
        for item in resolved_assumptions
        if str(item.get("reason") or "") != "client_value_not_supplied"
    ]
    model_links = [
        _json_copy(item)
        for item in source_allocations or []
        if isinstance(item, Mapping)
    ]
    if not model_links and isinstance(source_allocation, Mapping):
        model_links = [_json_copy(source_allocation)]
    if not model_links:
        inferred_link = _allocation_link_from_effective_input(
            request.get("effective_input")
        )
        if inferred_link is not None:
            model_links = [inferred_link]
    model_link = model_links[0] if len(model_links) == 1 else None
    return {
        "schema_version": AGENT_VIEW_SCHEMA_VERSION,
        "analysis_id": analysis_id,
        "created_at": created_at,
        "scenario": {
            "label": scenario.get("label") or request.get("scenario_label"),
            "summary": scenario.get("summary"),
            "rationale": scenario.get("rationale"),
            "applied_changes": _json_copy(request.get("applied_changes") or []),
        },
        "simulation": simulation,
        "permission": {
            "permitted_use": recommendation_evidence.get("permitted_use") or "none",
            "valid_for_reporting": recommendation_evidence.get("valid_for_reporting") is True,
            "valid_for_conclusion": recommendation_evidence.get("valid_for_conclusion") is True,
            "valid_for_recommendation": recommendation_evidence.get("valid_for_recommendation") is True,
            "conclusion_code": recommendation_evidence.get("conclusion_code"),
        },
        "claims": claims,
        "interpretations": _cashflow_interpretations(claims),
        "assumptions": _string_list(recommendation_evidence.get("assumptions")),
        "resolved_assumptions": resolved_assumptions,
        "assumption_summary": {
            "configured_defaults": configured_defaults,
            "effective_parameters": effective_parameters,
            "configured_default_count": len(configured_defaults),
            "effective_parameter_count": len(effective_parameters),
        },
        "model_links": {
            "source_allocation": model_link,
            "source_allocations": model_links,
            "allocation_linked": bool(model_links),
            "allocation_count": len(model_links),
            "relationship": (
                "Each linked Asset Allocation result supplies target weights only "
                "for its confirmed funded money-pool sleeve. LifeModel applies its "
                "own configured per-asset return, volatility, and correlation assumptions."
                if model_links
                else "Cash flow uses exact allocations already represented in the Client File."
            ),
        },
        "warnings": _string_list(recommendation_evidence.get("warnings")),
        "limitations": _cashflow_limitations(
            claims,
            status,
            simulation=simulation,
        ),
        "follow_up_policy": {
            "answer_without_rerun": [
                "reported metric meaning and relationship",
                "percentile and downside explanation",
                "first depletion or shortfall timing",
                "assumption and warning disclosure",
                "liquidity and shortfall interpretation",
                "reported p10, p50, and p90 cash-flow milestone values",
                "exact-year values already present in stored annual report series",
                "why the result is not a guarantee",
                "which exact allocation analyses were linked and how LifeModel used them",
            ],
            "rerun_required": [
                "changed retirement age, spending, income, life expectancy, or account balance",
                "new stress, expense, mortgage-payoff, or other supported scenario change",
                "a higher Monte Carlo path count",
                "a stale Client File fingerprint",
                "an annual report column that the original optimized run did not collect",
            ],
            "separate_model_required": [
                "exact monthly contribution optimization",
                "asset-allocation or security recommendation",
                "tax optimization not represented by the cash-flow model",
            ],
        },
    }


def _allocation_link_from_effective_input(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, Mapping):
        return None
    provenance = value.get("allocation_provenance")
    if not isinstance(provenance, Mapping):
        return None
    policies = [
        _json_copy(item)
        for item in provenance.get("policies") or []
        if isinstance(item, Mapping)
    ]
    if not policies:
        return None
    return {
        "analysis_id": None,
        "source": provenance.get("source"),
        "policies": policies,
        "cashflow_return_policy": provenance.get("return_policy"),
    }


def _cashflow_interpretations(claims: Mapping[str, Mapping[str, Any]]) -> list[Dict[str, Any]]:
    output: list[Dict[str, Any]] = []
    terminal = _claim_mapping(claims, "terminal_value_percentiles")
    terminal_p10 = _number(terminal.get("p10"))
    terminal_p50 = _number(terminal.get("p50"))
    if terminal_p10 is not None and terminal_p50 is not None and terminal_p10 < 0 <= terminal_p50:
        output.append(
            {
                "code": "downside_net_worth_below_zero_despite_nonnegative_median",
                "meaning": "The median does not represent lower-tail terminal-net-worth outcomes.",
                "evidence_refs": [_evidence_ref(claims, "terminal_value_percentiles")],
            }
        )
    shortfall = _claim_mapping(claims, "shortfall_percentiles")
    shortfall_p50 = _number(shortfall.get("p50"))
    if shortfall_p50 is not None and shortfall_p50 > 0:
        output.append(
            {
                "code": "median_terminal_shortfall_positive",
                "meaning": "The median path ends with modeled cash-flow shortfall debt.",
                "evidence_refs": [_evidence_ref(claims, "shortfall_percentiles")],
            }
        )
    minimum_liquidity = _claim_number(claims, "minimum_liquidity")
    if minimum_liquidity is not None and minimum_liquidity <= 0:
        output.append(
            {
                "code": "liquidity_floor_reached",
                "meaning": "The reported path statistic reaches the liquidity floor.",
                "evidence_refs": [_evidence_ref(claims, "minimum_liquidity")],
            }
        )
    if "success_probability" in claims and (
        "shortfall" in claims or "shortfall_percentiles" in claims
    ):
        output.append(
            {
                "code": "net_worth_success_differs_from_cashflow_shortfall",
                "meaning": "Net-worth success and unmet-cash-flow debt answer different questions and must be reviewed together.",
                "evidence_refs": [
                    _evidence_ref(claims, "success_probability"),
                    _evidence_ref(
                        claims,
                        "shortfall_percentiles" if "shortfall_percentiles" in claims else "shortfall",
                    ),
                ],
            }
        )
    if shortfall_p50 is not None and shortfall_p50 > 0 and minimum_liquidity is not None and minimum_liquidity <= 0:
        output.append(
            {
                "code": "additional_monthly_investment_requires_solver",
                "meaning": (
                    "This projection alone does not establish a monthly investment "
                    "amount; use the bounded contribution solver with explicit constraints."
                ),
                "evidence_refs": [
                    _evidence_ref(claims, "shortfall_percentiles"),
                    _evidence_ref(claims, "minimum_liquidity"),
                ],
            }
        )
    return output


def _cashflow_simulation_summary(
    claims: Mapping[str, Mapping[str, Any]],
    run_metadata: Mapping[str, Any],
) -> Dict[str, Any]:
    mode = str(run_metadata.get("simulation_mode") or "").strip().lower()
    path_count = _number(run_metadata.get("num_simulations"))
    if path_count is not None and (path_count < 1 or not path_count.is_integer()):
        path_count = None

    claim_sample_counts: set[int] = set()
    for key in ("first_depletion_year_distribution", "first_shortfall_year_distribution"):
        value = _claim_mapping(claims, key)
        sample_count = _number(value.get("sample_count"))
        if sample_count is not None and sample_count > 0 and sample_count.is_integer():
            claim_sample_counts.add(int(sample_count))
    if path_count is None and len(claim_sample_counts) == 1:
        path_count = float(next(iter(claim_sample_counts)))
    if mode not in {"deterministic", "monte_carlo"}:
        if path_count == 1:
            mode = "deterministic"
        elif path_count is not None and path_count > 1:
            mode = "monte_carlo"
        else:
            mode = "unknown"

    normalized_count = int(path_count) if path_count is not None else None
    return {
        "mode": mode,
        "path_count": normalized_count,
        "random_seed": run_metadata.get("random_seed"),
        "stochastic": mode == "monte_carlo",
        "success_semantics": (
            "binary_pass_fail_for_one_baseline_path"
            if mode == "deterministic"
            else "estimated_share_of_modeled_paths_meeting_the_success_rule"
            if mode == "monte_carlo"
            else "not_established"
        ),
    }


def _cashflow_limitations(
    claims: Mapping[str, Mapping[str, Any]],
    status: Mapping[str, Any],
    *,
    simulation: Mapping[str, Any],
) -> list[Dict[str, Any]]:
    output = [
        {
            "code": "monthly_contribution_not_in_snapshot",
            "meaning": (
                "This snapshot is one projection, not a goal-seeking result. The "
                "separate bounded contribution solver is required for a monthly amount."
            ),
        },
    ]
    mode = simulation.get("mode")
    count = simulation.get("path_count")
    if mode == "deterministic":
        output.insert(
            0,
            {
                "code": "deterministic_single_path",
                "meaning": (
                    "This is one baseline trajectory, not a Monte Carlo distribution. "
                    "Its success value is a binary pass/fail result, and repeated "
                    "p10/p50/p90 values do not represent distinct outcomes."
                ),
                "sample_count": 1,
            },
        )
    elif mode == "monte_carlo":
        output.insert(
            0,
            {
                "code": "simulation_not_guarantee",
                "meaning": (
                    "Monte Carlo probabilities and percentiles are estimates, not guarantees."
                ),
            },
        )
    else:
        output.insert(
            0,
            {
                "code": "projection_not_guarantee",
                "meaning": "The modeled projection is an estimate, not a guarantee.",
            },
        )
    if mode == "monte_carlo" and isinstance(count, int) and count > 0:
        output.append(
            {
                "code": "finite_monte_carlo_sample",
                "meaning": f"Sampling uncertainty remains because this result uses {count} modeled paths.",
                "sample_count": count,
            }
        )
    if status.get("analysis_grade") == "interactive_estimate":
        output.append(
            {
                "code": "estimate_only",
                "meaning": "The result may support factual reporting but not a recommendation.",
            }
        )
    return output


def _claim_mapping(claims: Mapping[str, Mapping[str, Any]], key: str) -> Dict[str, Any]:
    claim = claims.get(key)
    value = claim.get("value") if isinstance(claim, Mapping) else None
    return dict(value) if isinstance(value, Mapping) else {}


def _claim_number(claims: Mapping[str, Mapping[str, Any]], key: str) -> Optional[float]:
    claim = claims.get(key)
    return _number(claim.get("value")) if isinstance(claim, Mapping) else None


def _evidence_ref(claims: Mapping[str, Mapping[str, Any]], key: str) -> str:
    claim = claims.get(key)
    return str(claim.get("evidence_ref") or "") if isinstance(claim, Mapping) else ""


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))

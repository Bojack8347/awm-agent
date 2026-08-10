from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from advisor.agents.quant_contracts._shared import _finite_numeric
from advisor.agents.quant_contracts.cashflow_narrative import (
    _cashflow_evidence_sample_count,
    _has_saved_asset_allocation_analysis,
    _has_saved_cashflow_analysis,
    _render_cashflow_evidence_interpretation,
    _render_cashflow_single_path_client_summary,
    _select_cashflow_assumptions,
    _tool_display_name,
)
from advisor.agents.quant_contracts.claim_rendering import (
    _format_claim_value,
    _render_evidence_claim,
    _render_evidence_claims,
)
from advisor.agents.quant_contracts.evidence import _evidence_from_tool_results
from advisor.agents.quant_contracts.models import QuantEvidenceClaim, QuantEvidenceEnvelope


def render_quantitative_reporting_fallback(
    tool_results: Sequence[Dict[str, Any]],
    *,
    user_message: str = "",
) -> Optional[str]:
    """Render validated facts when model-authored narration exceeds its permission.

    This path is deliberately deterministic: it can preserve useful reporting-grade
    output, but it never invents a feasibility conclusion or recommendation.
    """

    evidence = _evidence_from_tool_results(tool_results)
    if not evidence or not all(item.valid_for_reporting for item in evidence):
        return None
    allocation_proposal = _render_asset_allocation_proposal(
        tool_results,
        evidence,
        user_message=user_message,
    )
    if allocation_proposal:
        return allocation_proposal
    focused = _render_quantitative_followup_fallback(
        tool_results,
        evidence,
        user_message=user_message,
    )
    if focused:
        return focused
    has_single_path_cashflow = any(
        item.tool == "run_cashflow_projection"
        and _cashflow_evidence_sample_count(item) == 1
        for item in evidence
    )
    if has_single_path_cashflow and len(evidence) == 1:
        client_summary = _render_cashflow_single_path_client_summary(evidence[0])
        if client_summary:
            return client_summary
    has_monte_carlo_cashflow = any(
        item.tool == "run_cashflow_projection"
        and (_cashflow_evidence_sample_count(item) or 0) > 1
        for item in evidence
    )
    lines = (
        [
            "Cash-flow baseline — deterministic estimate",
            "This run models one baseline path, not a range of possible market "
            "outcomes. Its success field is a pass/fail test, not a Monte Carlo "
            "probability, and repeated percentile values are not separate scenarios.",
        ]
        if has_single_path_cashflow
        else [
            "Retirement projection — modeled range",
            "This result shows a range across modeled paths. Read p10 as a weaker "
            "modeled outcome, p50 as the midpoint, and p90 as a stronger modeled "
            "outcome—not as promises or separate recommendations.",
        ]
        if has_monte_carlo_cashflow
        else [
            "A reporting-only summary of validated model values is below. "
            "It does not decide the planning question."
        ]
    )
    for envelope in evidence:
        single_path = (
            envelope.tool == "run_cashflow_projection"
            and _cashflow_evidence_sample_count(envelope) == 1
        )
        rendered = _render_evidence_claims(
            envelope,
            deterministic_single_path=single_path,
        )
        if not rendered:
            return None
        lines.append(
            "\nWhat the model shows:"
            if single_path
            else f"\n{_tool_display_name(envelope.tool)}:"
        )
        lines.extend(f"- {item}." for item in rendered)
        if envelope.tool == "run_cashflow_projection":
            interpretation = _render_cashflow_evidence_interpretation(
                envelope,
                deterministic_single_path=single_path,
            )
            if interpretation:
                lines.append(
                    "\nHow to read this:"
                    if single_path
                    else "\nEvidence interpretation:"
                )
                lines.extend(f"- {item}." for item in interpretation)
            if envelope.assumptions:
                lines.append("\nApplied model assumptions:")
                if single_path:
                    lines.append(
                        "- Verify these defaults before using the estimate for a "
                        "planning decision."
                    )
                lines.extend(
                    f"- {assumption.rstrip('.')}."
                    for assumption in _select_cashflow_assumptions(
                        envelope.assumptions
                    )
                    if assumption.strip()
                )
            if _has_saved_cashflow_analysis(tool_results):
                lines.append(
                    "\nSaved result: this validated analysis is stored for unchanged "
                    "follow-up questions, so the cash-flow model does not need to rerun "
                    "just to explain its values, timing, or assumptions."
                )
            lines.append("\nNext analysis step:")
            lines.append(
                "- Verify the high-impact inputs and defaults above before treating this "
                "estimate as planning guidance. Rerun only for changed inputs or a "
                "stochastic scenario; use the bounded contribution solver for an exact "
                "monthly savings target."
            )
        elif envelope.tool == "audit_cashflow_analysis":
            audit_result = next(
                (
                    item.get("full_result")
                    for item in reversed(list(tool_results))
                    if isinstance(item, dict)
                    and item.get("tool") == "audit_cashflow_analysis"
                    and isinstance(item.get("full_result"), dict)
                ),
                {},
            )
            audit_status = str(audit_result.get("audit_status") or "unknown")
            lines.append(f"\nAudit status: {audit_status}.")
            findings = [
                item
                for item in audit_result.get("checks") or []
                if isinstance(item, dict)
                and item.get("status") in {"failed", "not_tested"}
            ]
            if findings:
                lines.append("\nFailed or untested reconciliation checks:")
                lines.extend(
                    f"- {item.get('check_id')}: {str(item.get('meaning') or '').rstrip('.')}."
                    for item in findings
                )
            lines.append(
                "- Audit scope: immutable stored evidence only; LifeModel was not rerun "
                "and the audit does not choose or change a financial plan."
            )
        elif envelope.tool == "query_wolfram_alpha" and envelope.warnings:
            lines.append("\nExternal computation limits:")
            lines.extend(
                f"- {warning.rstrip('.')}."
                for warning in envelope.warnings
                if warning.strip()
            )
        elif envelope.tool == "run_asset_allocation":
            if envelope.valid_for_conclusion:
                lines.append(
                    "- Validated conclusion: the allocation constraints were verified by the "
                    "model; this read-only result does not authorize implementation."
                )
            lines.append("\nHow to use this result:")
            lines.append(
                "- Expected return and volatility describe the model's long-run assumptions, "
                "not a forecast for the next year. Category and security weights show how the "
                "modeled risk is distributed."
            )
            lines.append(
                "- Useful follow-up review: compare the modeled return, volatility, and holdings "
                "with the signed pool's horizon, liquidity terms, exclusions, and tax constraints "
                "before any implementation decision."
            )
            if _has_saved_asset_allocation_analysis(tool_results):
                lines.append(
                    "- Saved result: this immutable allocation analysis can answer unchanged "
                    "follow-up questions without rerunning the optimizer."
                )
    return "\n".join(lines)


def _render_asset_allocation_proposal(
    tool_results: Sequence[Dict[str, Any]],
    evidence: Sequence[QuantEvidenceEnvelope],
    *,
    user_message: str,
) -> Optional[str]:
    prompt = " ".join(str(user_message or "").lower().split())
    if not any(term in prompt for term in ("proposal", "proposed allocation", "prepare the allocation")):
        return None
    allocation = next(
        (
            item
            for item in reversed(list(evidence))
            if item.tool == "run_asset_allocation" and item.valid_for_recommendation
        ),
        None,
    )
    result = next(
        (
            item
            for item in reversed(list(tool_results))
            if isinstance(item, dict)
            and item.get("tool") == "run_asset_allocation"
            and item.get("valid_for_recommendation") is True
        ),
        None,
    )
    if allocation is None or result is None:
        return None
    full_result = result.get("full_result") if isinstance(result.get("full_result"), dict) else {}
    securities = full_result.get("securities") if isinstance(full_result.get("securities"), list) else []
    claims = {claim.metric_key: claim for claim in allocation.claims}
    required_metrics = [
        claims.get("portfolio_expected_return_annual_decimal"),
        claims.get("portfolio_expected_volatility_annual_decimal"),
    ]
    rendered_metrics = [
        rendered
        for claim in required_metrics
        if claim is not None
        if (rendered := _render_evidence_claim(allocation.tool, claim))
    ]
    if len(rendered_metrics) != 2 or not securities:
        return None

    lines = [
        "Proposed allocation only — not executed and no money was moved:",
        *(f"- {item}." for item in rendered_metrics),
        "- Holdings:",
    ]
    for index, security in enumerate(securities):
        if not isinstance(security, dict):
            continue
        identifier = str(
            security.get("ticker") or security.get("isin") or index
        ).strip()
        weight = claims.get(f"security.{identifier}.weight")
        amount = claims.get(f"security.{identifier}.amount")
        if weight is None or amount is None:
            return None
        weight_display = _format_claim_value(weight.value, weight.unit)
        amount_display = _format_claim_value(amount.value, amount.unit)
        if not weight_display or not amount_display:
            return None
        label = str(security.get("asset_class") or identifier).strip()
        if identifier and identifier != label:
            label = f"{label} ({identifier})"
        lines.append(
            f"  - {label}: {weight_display}, {amount_display} "
            f"[evidence: run_asset_allocation/{weight.claim_id}; "
            f"run_asset_allocation/{amount.claim_id}]."
        )
    lines.append(
        "- Expected return and volatility are model estimates, not guarantees."
    )
    return "\n".join(lines)


def _render_quantitative_followup_fallback(
    tool_results: Sequence[Dict[str, Any]],
    evidence: Sequence[QuantEvidenceEnvelope],
    *,
    user_message: str,
) -> Optional[str]:
    """Answer common stored-result follow-ups without dumping the whole report."""

    prompt = " ".join(str(user_message or "").lower().split())
    if not prompt:
        return None
    calculator = next(
        (
            item
            for item in reversed(list(evidence))
            if item.tool
            in {"calculate_cashflow_metrics", "calculate_financial_math"}
        ),
        None,
    )
    if calculator is not None:
        tool_result = next(
            (
                item
                for item in reversed(list(tool_results))
                if isinstance(item, dict) and item.get("tool") == calculator.tool
            ),
            {},
        )
        full_result = (
            tool_result.get("full_result")
            if isinstance(tool_result.get("full_result"), dict)
            else {}
        )
        claims = {claim.metric_key: claim for claim in calculator.claims}
        comparison_context = (
            full_result.get("comparison_context")
            if isinstance(full_result.get("comparison_context"), dict)
            else {}
        )
        crosses_zero = comparison_context.get("crosses_zero") is True
        is_v2_financial_math = (
            calculator.tool == "calculate_financial_math"
            and full_result.get("schema_version") == "awm.financial_math.v2"
        )
        if calculator.tool == "calculate_cashflow_metrics":
            ordered_keys = (
                (
                    "comparison.signed_difference",
                    "calculation_result",
                    "primary_operand",
                    "secondary_operand",
                )
                if crosses_zero
                else (
                    "calculation_result",
                    "primary_operand",
                    "secondary_operand",
                )
            )
        else:
            ordered_keys = (
                (
                    "comparison.signed_difference",
                    "calculation_result",
                    "input.primary_value",
                    "input.secondary_value",
                    "input.annual_rate_decimal",
                    "input.periods",
                    "input.payments_per_year",
                )
                if crosses_zero
                else (
                    "calculation_result",
                    "input.primary_value",
                    "input.secondary_value",
                    "input.annual_rate_decimal",
                    "input.periods",
                    "input.payments_per_year",
                )
            )
        if is_v2_financial_math:
            rendered = [
                item
                for claim in calculator.claims
                if claim.metric_key.startswith("calculation_result")
                if (
                    item := _render_evidence_claim(
                        calculator.tool,
                        claim.model_copy(update={"metric_key": "calculation_result"}),
                    )
                )
            ][:10]
        else:
            rendered = [
                item
                for key in ordered_keys
                if (claim := claims.get(key)) is not None
                if (item := _render_evidence_claim(calculator.tool, claim))
            ]
        if rendered:
            heading = (
                "Calculation from the stored cash-flow result"
                if calculator.tool == "calculate_cashflow_metrics"
                else "Validated financial calculation"
            )
            lines = [heading]
            formula = str(full_result.get("formula") or "").strip()
            if formula:
                lines.append(f"- Formula: `{formula}`.")
            lines.extend(f"- {item}." for item in rendered)
            if calculator.tool == "calculate_cashflow_metrics":
                analysis_id = str(full_result.get("analysis_id") or "").strip()
                if analysis_id:
                    lines.append(f"- Stored analysis: {analysis_id}; LifeModel was not rerun.")
            lines.append("- Scope: reporting-only arithmetic; no planning decision was made.")
            return "\n".join(lines)
    comparison = next(
        (
            item
            for item in reversed(list(evidence))
            if item.tool == "compare_quant_analyses"
        ),
        None,
    )
    if comparison is not None:
        tool_result = next(
            (
                item
                for item in reversed(list(tool_results))
                if isinstance(item, dict)
                and item.get("tool") == "compare_quant_analyses"
            ),
            {},
        )
        full_result = (
            tool_result.get("full_result")
            if isinstance(tool_result.get("full_result"), dict)
            else {}
        )
        rows = [
            row
            for row in full_result.get("deltas") or []
            if isinstance(row, dict)
        ]
        topic_fragments = {
            "expected_return": ("expected return", "return"),
            "volatility": ("volatility", "risk"),
            "total_investment": ("total", "amount", "capital"),
            "active_sleeve": ("active",),
            "passive_sleeve": ("passive",),
            "equity": ("equity", "stock"),
            "cash": ("cash",),
            "fixed_income": ("bond", "fixed income"),
        }
        requested_fragments = {
            fragment
            for fragment, markers in topic_fragments.items()
            if any(marker in prompt for marker in markers)
        }
        if requested_fragments:
            relevant_rows = [
                row
                for row in rows
                if any(
                    fragment
                    in str(row.get("metric_key") or "").lower()
                    for fragment in requested_fragments
                )
            ]
            if relevant_rows:
                rows = relevant_rows
        rendered_rows: List[str] = []
        comparison_claims = {
            claim.metric_key: claim for claim in comparison.claims
        }
        for row in rows[:4]:
            unit = str(row.get("unit") or "").strip()
            base_display = _format_claim_value(row.get("base_value"), unit)
            comparison_display = _format_claim_value(
                row.get("comparison_value"), unit
            )
            delta_display = _format_claim_value(row.get("delta"), unit)
            if not all((base_display, comparison_display, delta_display)):
                continue
            label = (
                str(row.get("metric_key") or "modeled metric")
                .replace("portfolio_", "")
                .replace("_annual_decimal", "")
                .replace("_", " ")
                .strip()
                .capitalize()
            )
            claim = comparison_claims.get(
                str(row.get("delta_metric_key") or "")
            )
            reference = (
                f" [evidence: compare_quant_analyses/{claim.claim_id}]"
                if claim is not None
                else ""
            )
            rendered_rows.append(
                f"- {label}: base {base_display}; comparison "
                f"{comparison_display}; change {delta_display} "
                f"(comparison minus base){reference}."
            )
        if rendered_rows:
            base_id = str(full_result.get("base_analysis_id") or "").strip()
            comparison_id = str(
                full_result.get("comparison_analysis_id") or ""
            ).strip()
            lines = ["Comparison of the two saved analyses"]
            if base_id and comparison_id:
                lines.append(f"- Base: {base_id}; comparison: {comparison_id}.")
            lines.extend(rendered_rows)
            lines.append(
                "- Meaning: these are exact arithmetic differences between the "
                "stored results; they do not establish that one changed input "
                "caused the difference."
            )
            return "\n".join(lines)
    solver = next(
        (
            item
            for item in reversed(list(evidence))
            if item.tool == "solve_cashflow_contribution"
        ),
        None,
    )
    if solver is not None:
        tool_result = next(
            (
                item
                for item in reversed(list(tool_results))
                if isinstance(item, dict)
                and item.get("tool") == "solve_cashflow_contribution"
            ),
            {},
        )
        full_result = (
            tool_result.get("full_result")
            if isinstance(tool_result.get("full_result"), dict)
            else {}
        )
        status = str(full_result.get("status") or "").strip()
        status_text = {
            "bounded_solution": (
                "The model found a tested boundary within the stated monthly tolerance."
            ),
            "search_ceiling_feasible": (
                "The tested ceiling still met the stated constraints, so this run "
                "did not establish the true maximum."
            ),
            "baseline_satisfies_target": (
                "The zero-additional-contribution baseline met the stated target."
            ),
            "baseline_infeasible": (
                "The baseline failed at least one stated constraint."
            ),
            "target_not_reached_within_search_ceiling": (
                "The target was not reached within the stated search ceiling."
            ),
        }.get(status)
        if status_text:
            claims = {claim.metric_key: claim for claim in solver.claims}
            boundary_interpretation = (
                full_result.get("boundary_interpretation")
                if isinstance(full_result.get("boundary_interpretation"), dict)
                else {}
            )
            preferred_keys = (
                "selected_monthly_contribution",
                "selected.success_probability",
                "selected.p10_minimum_liquidity",
                "search_tolerance",
                "input.maximum_monthly_contribution",
            )
            rendered = [
                item
                for key in preferred_keys
                if (claim := claims.get(key)) is not None
                if (item := _render_evidence_claim(solver.tool, claim))
            ][:5]
            lines = ["Bounded contribution search", status_text]
            explanation = str(
                boundary_interpretation.get("explanation") or ""
            ).strip()
            if explanation:
                lines.append(f"- Boundary meaning: {explanation}")
            feasible_min = claims.get("known_feasible_interval.minimum")
            feasible_max = claims.get("known_feasible_interval.maximum")
            if feasible_min is not None and feasible_max is not None:
                minimum_display = _format_claim_value(
                    feasible_min.value,
                    feasible_min.unit,
                )
                maximum_display = _format_claim_value(
                    feasible_max.value,
                    feasible_max.unit,
                )
                if minimum_display and maximum_display:
                    lines.append(
                        "- Known tested-feasible interval: "
                        f"{minimum_display} to {maximum_display}."
                    )
            transition_lower = claims.get("transition_interval.lower")
            transition_upper = claims.get("transition_interval.upper")
            if transition_lower is not None and transition_upper is not None:
                lower_display = _format_claim_value(
                    transition_lower.value,
                    transition_lower.unit,
                )
                upper_display = _format_claim_value(
                    transition_upper.value,
                    transition_upper.unit,
                )
                if lower_display and upper_display:
                    lines.append(
                        "- Feasibility transition is bracketed between "
                        f"{lower_display} and {upper_display}."
                    )
            failed_constraints = [
                str(item.get("label") or "").strip()
                for item in boundary_interpretation.get(
                    "binding_failed_constraints"
                )
                or []
                if isinstance(item, dict) and str(item.get("label") or "").strip()
            ]
            if failed_constraints:
                lines.append(
                    "- Binding failed constraint"
                    + ("s" if len(failed_constraints) > 1 else "")
                    + ": "
                    + ", ".join(failed_constraints)
                    + "."
                )
            for meaning_key in (
                "zero_boundary_meaning",
                "search_ceiling_meaning",
            ):
                meaning = str(
                    boundary_interpretation.get(meaning_key) or ""
                ).strip()
                if meaning:
                    lines.append(f"- {meaning}")
            lines.extend(f"- {item}." for item in rendered)
            lines.append(
                "- Meaning: this reports only the model-tested boundary under "
                "the supplied constraints; it does not tell the client what to save."
            )
            return "\n".join(lines)
    stored_result_followup = any(
        phrase in prompt
        for phrase in (
            "using only",
            "stored result",
            "stored projection",
            "stored allocation",
            "completed projection",
            "completed allocation",
            "we just completed",
            "we just discussed",
            "do not rerun",
            "don't rerun",
            "without rerun",
            "without rerunning",
            "prior projection",
            "prior allocation",
            "existing projection",
            "existing allocation",
        )
    )
    has_cashflow_evidence = any(
        item.tool in {"run_cashflow_projection", "get_cashflow_analysis"}
        for item in evidence
    )
    has_allocation_evidence = any(
        item.tool in {"run_asset_allocation", "get_asset_allocation_analysis"}
        for item in evidence
    )
    single_model_evidence = not (
        has_cashflow_evidence and has_allocation_evidence
    )

    risk = next(
        (
            item
            for item in reversed(list(evidence))
            if item.tool == "analyze_portfolio_risk"
        ),
        None,
    )
    if risk is not None and any(
        term in prompt
        for term in (
            "risk contributor",
            "risk contribution",
            "risk driver",
            "drives the risk",
            "driving risk",
        )
    ):
        risk_result = next(
            (
                item.get("full_result")
                for item in reversed(list(tool_results))
                if isinstance(item, dict)
                and item.get("tool") == "analyze_portfolio_risk"
                and isinstance(item.get("full_result"), dict)
            ),
            {},
        )
        rows = (
            risk_result.get("risk_contributions")
            if isinstance(risk_result, dict)
            and isinstance(risk_result.get("risk_contributions"), list)
            else []
        )
        ranked: List[tuple[float, int, Dict[str, Any]]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            contribution = _finite_numeric(
                row.get("percentage_of_total_variance")
            )
            if contribution is None:
                continue
            ranked.append((contribution, index, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        if ranked:
            lines = [
                "What drives the modeled portfolio risk",
                "The largest contributors to total modeled variance are:",
            ]
            for contribution, index, row in ranked[:3]:
                asset_class = str(row.get("asset_class") or f"Asset class {index + 1}")
                weight = _finite_numeric(row.get("weight"))
                detail = (
                    f"{asset_class}: {contribution * 100:.2f}% of modeled total "
                    "variance"
                )
                references = [
                    (
                        "analyze_portfolio_risk/"
                        f"risk_contribution.{index}.percentage_of_total_variance"
                    )
                ]
                if weight is not None:
                    detail += f" at a {weight * 100:.2f}% portfolio weight"
                    references.append(
                        "analyze_portfolio_risk/"
                        f"risk_contribution.{index}.weight"
                    )
                lines.append(
                    f"- {detail} [evidence: {'; '.join(references)}]."
                )
            lines.extend(
                [
                    "",
                    "How to read this:",
                    "- A risk contribution is a share of modeled variance. It is not the "
                    "same as portfolio weight, expected loss, or a prediction of which "
                    "holding will fall next.",
                    "- This is analysis of the completed allocation only; it does not "
                    "change the allocation or authorize implementation.",
                ]
            )
            if risk.warnings:
                lines.append("")
                lines.append("Model limitations:")
                lines.extend(
                    f"- {warning}"
                    for warning in risk.warnings
                    if str(warning).strip()
                )
            return "\n".join(lines)

    lineage_requested = (
        "allocation" in prompt
        and any(
            phrase in prompt
            for phrase in (
                "fed the",
                "fed into",
                "linked",
                "which exact",
                "expected return directly",
                "how lifemodel",
            )
        )
    )
    if lineage_requested:
        for result in reversed(list(tool_results)):
            if not isinstance(result, dict) or result.get("tool") not in {
                "run_cashflow_projection",
                "get_cashflow_analysis",
            }:
                continue
            view = (
                result.get("cashflow_agent_view")
                if isinstance(result.get("cashflow_agent_view"), dict)
                else {}
            )
            links = (
                view.get("model_links")
                if isinstance(view.get("model_links"), dict)
                else {}
            )
            source = (
                links.get("source_allocation")
                if isinstance(links.get("source_allocation"), dict)
                else result.get("source_allocation")
            )
            if not isinstance(source, dict):
                continue
            analysis_id = str(source.get("analysis_id") or "").strip()
            if not analysis_id:
                continue
            return "\n".join(
                [
                    "Using the stored validated results; neither model was rerun:",
                    f"- Linked allocation analysis: {analysis_id}.",
                    "- Asset Allocation supplied the exact target weights for the addressed "
                    "funded sleeve.",
                    "- LifeModel did not use the optimizer's expected-return figure directly. "
                    "It applied those weights with LifeModel's configured per-asset return, "
                    "volatility, and correlation assumptions.",
                ]
            )

    cashflow = next(
        (
            item
            for item in reversed(list(evidence))
            if item.tool in {"run_cashflow_projection", "get_cashflow_analysis"}
        ),
        None,
    )
    cashflow_topics = any(
        term in prompt
        for term in (
            "p10",
            "p50",
            "p90",
            "percentile",
            "assumption",
            "default",
            "shortfall",
            "depletion",
            "liquidity",
            "without rerun",
            "do not rerun",
            "stored projection",
            "completed projection",
            "milestone",
            "trajectory",
        )
    )
    if (
        cashflow is not None
        and cashflow_topics
        and stored_result_followup
        and single_model_evidence
    ):
        claims = {claim.metric_key: claim for claim in cashflow.claims}
        requested_keys: List[str] = []
        if any(term in prompt for term in ("p10", "p50", "p90", "percentile")):
            requested_keys.extend(
                ("terminal_value_percentiles", "shortfall_percentiles")
            )
        if any(term in prompt for term in ("milestone", "trajectory", "over time")):
            requested_keys.append("milestone_percentile_trajectory")
        if "success" in prompt:
            requested_keys.append("success_probability")
        if "shortfall" in prompt:
            requested_keys.extend(("shortfall", "first_shortfall_year_distribution"))
        if "depletion" in prompt:
            requested_keys.append("first_depletion_year_distribution")
        if any(term in prompt for term in ("liquidity", "reserve")):
            requested_keys.extend(("minimum_liquidity", "reserve_breach_probability"))
        requested_keys = list(dict.fromkeys(requested_keys))
        rendered = [
            item
            for key in requested_keys
            if (claim := claims.get(key)) is not None
            if (item := _render_evidence_claim(cashflow.tool, claim))
        ]
        lines = ["Using the stored validated cash-flow result; LifeModel was not rerun:"]
        lines.extend(f"- {item}." for item in rendered)
        milestone_claim = claims.get("milestone_percentile_trajectory")
        if (
            milestone_claim is not None
            and milestone_claim.metric_key in requested_keys
        ):
            lines.extend(
                f"- {item}."
                for item in _render_cashflow_milestone_claim(
                    cashflow.tool,
                    milestone_claim,
                    prompt=prompt,
                )
            )
        if any(term in prompt for term in ("p10", "p50", "p90", "percentile", "shortfall")):
            interpretations = _render_cashflow_evidence_interpretation(cashflow)
            lines.extend(f"- {item}." for item in interpretations)
        if any(term in prompt for term in ("assumption", "default")):
            defaults = [
                assumption
                for assumption in cashflow.assumptions
                if " defaults to " in assumption.lower()
            ]
            if defaults:
                lines.append("Configured defaults used by this run:")
                lines.extend(f"- {item.rstrip('.')}." for item in defaults)
        if any(term in prompt for term in ("next", "compare", "comparison")):
            lines.append(
                "- Supported comparison: move retirement two years later, hold every other "
                "confirmed input and model assumption constant, and rerun."
            )
        if len(lines) > 1:
            return "\n".join(lines)

    allocation = next(
        (
            item
            for item in reversed(list(evidence))
            if item.tool in {"run_asset_allocation", "get_asset_allocation_analysis"}
        ),
        None,
    )
    allocation_topics = any(
        term in prompt
        for term in (
            "holding",
            "security",
            "active",
            "passive",
            "sleeve",
            "target",
            "tolerance",
            "volatility",
            "expected return",
            "reconcile",
            "exclusion",
            "without rerun",
            "do not rerun",
            "stored allocation",
            "completed allocation",
        )
    )
    if (
        allocation is not None
        and allocation_topics
        and stored_result_followup
        and single_model_evidence
    ):
        claims = {claim.metric_key: claim for claim in allocation.claims}
        selected: List[QuantEvidenceClaim] = []
        if "expected return" in prompt:
            claim = claims.get("portfolio_expected_return_annual_decimal")
            if claim:
                selected.append(claim)
        if any(term in prompt for term in ("target", "tolerance", "volatility")):
            for key in (
                "portfolio_expected_volatility_annual_decimal",
                "target_volatility_annual_decimal",
                "target_volatility_difference_bps",
                "target_volatility_tolerance_bps",
                "target_volatility_passed",
            ):
                if (claim := claims.get(key)) is not None:
                    selected.append(claim)
        if any(term in prompt for term in ("active", "passive", "sleeve")):
            for key in ("active_sleeve_weight", "passive_sleeve_weight"):
                if (claim := claims.get(key)) is not None:
                    selected.append(claim)
        if any(term in prompt for term in ("holding", "security")):
            weight_claims = [
                claim
                for claim in allocation.claims
                if claim.metric_key.startswith("security.")
                and claim.metric_key.endswith(".weight")
                and _finite_numeric(claim.value) is not None
            ]
            weight_claims.sort(
                key=lambda claim: _finite_numeric(claim.value) or 0.0,
                reverse=True,
            )
            limit = len(weight_claims) if "every" in prompt or "all " in prompt else 5
            selected.extend(weight_claims[:limit])
        rendered = [
            item
            for claim in selected
            if (item := _render_evidence_claim(allocation.tool, claim))
        ]
        if rendered:
            lines = [
                "Using the stored validated allocation; the optimizer was not rerun:"
            ]
            lines.extend(f"- {item}." for item in rendered)
            lines.append(
                "- Expected return and volatility are model estimates, not guarantees; "
                "this read-only result does not execute trades."
            )
            return "\n".join(lines)
    return None


def _render_cashflow_milestone_claim(
    tool_name: str,
    claim: QuantEvidenceClaim,
    *,
    prompt: str,
) -> List[str]:
    """Render bounded stored trajectory rows without doing new arithmetic."""

    if not isinstance(claim.value, dict):
        return []
    requested_series = []
    if "net worth" in prompt or not any(
        marker in prompt for marker in ("bank", "liquid", "shortfall")
    ):
        requested_series.append(("net_worth", "net worth"))
    if any(marker in prompt for marker in ("bank", "liquid", "cash balance")):
        requested_series.append(("bank_balance", "bank balance"))
    if "shortfall" in prompt:
        requested_series.append(
            ("cashflow_shortfall_debt", "cash-flow shortfall debt")
        )
    reference = f"[evidence: {tool_name}/{claim.claim_id}]"
    output: List[str] = []
    for year, row in claim.value.items():
        if not isinstance(row, dict):
            continue
        parts: List[str] = []
        for key, label in requested_series:
            values = row.get(key)
            if not isinstance(values, dict):
                continue
            percentiles = []
            for percentile in ("p10", "p50", "p90"):
                number = _finite_numeric(values.get(percentile))
                if number is None:
                    continue
                sign = "-" if number < 0 else ""
                percentiles.append(
                    f"{percentile} {sign}${abs(number):,.2f}"
                )
            if percentiles:
                parts.append(f"{label}: " + ", ".join(percentiles))
        if parts:
            output.append(f"{year} - {'; '.join(parts)} {reference}")
    return output


def render_quantitative_missing_data_fallback(
    tool_results: Sequence[Dict[str, Any]],
) -> Optional[str]:
    """Explain a blocked cash-flow run using its deterministic missing-input list."""

    labels = {
        "current_age": "current age",
        "retirement_age": "planned retirement age",
        "annual_income": "annual household income",
        "annual_spending": "annual household spending",
        "starting_assets": "starting asset balances",
        "life_expectancy": "planning life expectancy",
        "cash_balance": "cash balance",
        "brokerage_balance": "brokerage balance",
        "retirement_balance": "retirement-account balance",
        "education_goal_amount": "education goal amount",
        "education_horizon_years": "education goal horizon",
    }
    mortgage_labels = {
        "home_value": "the current home value",
        "home_appreciation_rate": "an annual home-value growth assumption",
        "mortgage_interest_rate": "the mortgage interest rate",
        "mortgage_remaining_term_years": "the remaining mortgage term",
        "mortgage_type": "the mortgage type",
        "annual_spending_includes_mortgage": (
            "whether annual spending includes mortgage principal and interest"
        ),
    }
    for result in reversed(list(tool_results)):
        if not isinstance(result, dict) or result.get("tool") != "run_cashflow_projection":
            continue
        analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
        raw_missing = result.get("missing_data") or analysis.get("missing_data") or []
        missing = []
        for item in raw_missing:
            marker = str(item).strip()
            if not marker:
                continue
            if marker.startswith("missing_asset_allocation:"):
                account_kind = marker.split(":", 1)[1].replace("_", " ")
                missing.append(f"how your {account_kind} account is currently invested")
            elif marker.startswith("missing_mortgage_input:"):
                mortgage_field = marker.split(":", 1)[1]
                missing.append(
                    mortgage_labels.get(
                        mortgage_field,
                        mortgage_field.replace("_", " "),
                    )
                )
            elif marker.startswith("unsupported_existing_mortgage_type:"):
                mortgage_type = marker.split(":", 1)[1].replace("_", " ")
                missing.append(
                    f"a supported fixed-rate opening mortgage instead of {mortgage_type}"
                )
            else:
                missing.append(labels.get(marker, marker.replace("_", " ")))
        if not missing:
            # A later successful rerun supersedes an earlier blocked attempt in
            # the same turn. Never revive the stale missing-input question.
            return None
        if len(missing) == 1:
            missing_text = missing[0]
        else:
            missing_text = ", ".join(missing[:-1]) + f", and {missing[-1]}"
        next_question = str(
            result.get("next_question") or analysis.get("next_question") or ""
        ).strip()
        raw_markers = {str(item).strip() for item in raw_missing}
        mortgage_markers = {
            marker
            for marker in raw_markers
            if marker.startswith("missing_mortgage_input:")
            or marker.startswith("unsupported_existing_mortgage_type:")
        }
        if mortgage_markers:
            mortgage_missing = []
            for marker in raw_missing:
                marker_text = str(marker).strip()
                if marker_text.startswith("missing_mortgage_input:"):
                    field_name = marker_text.split(":", 1)[1]
                    mortgage_missing.append(
                        mortgage_labels.get(field_name, field_name.replace("_", " "))
                    )
                elif marker_text.startswith("unsupported_existing_mortgage_type:"):
                    mortgage_type = marker_text.split(":", 1)[1].replace("_", " ")
                    mortgage_missing.append(
                        f"a supported fixed-rate opening mortgage instead of {mortgage_type}"
                    )
            if len(mortgage_missing) == 1:
                mortgage_missing_text = mortgage_missing[0]
            else:
                mortgage_missing_text = (
                    ", ".join(mortgage_missing[:-1])
                    + f", and {mortgage_missing[-1]}"
                )
            allocation_note = ""
            missing_allocations = [
                item
                for item in missing
                if item.startswith("how your ") and item.endswith(" currently invested")
            ]
            if missing_allocations:
                if len(missing_allocations) == 1:
                    allocation_text = missing_allocations[0]
                else:
                    allocation_text = (
                        ", ".join(missing_allocations[:-1])
                        + f" and {missing_allocations[-1]}"
                    )
                allocation_note = (
                    " A full projection also needs "
                    + allocation_text
                    + "."
                )
            return (
                "I can map this out once we fill in "
                f"{mortgage_missing_text}. Please share those details so I can include the "
                "mortgage accurately."
                f"{allocation_note}"
            )
        if len(missing) == 1 and next_question:
            return (
                "I’m nearly ready to map this out. I still need "
                f"{missing_text}. {next_question}"
            )
        return (
            "I’m nearly ready to map this out. I still need "
            f"{missing_text}. Please share those investment-mix details as approximate "
            "percentages by asset class, then I’ll continue."
        )
    return None


def render_asset_allocation_failure_fallback(
    tool_results: Sequence[Dict[str, Any]],
) -> Optional[str]:
    """Explain an allocation block using the deterministic adapter error."""

    messages = {
        "assessment_not_signed": (
            "The investment assessment isn’t signed yet, so I can’t prepare an allocation "
            "from it. Please complete sign-off first, then we can continue."
        ),
        "signed_assessment_stale": (
            "The signed investment assessment is out of date, so I can’t reuse it for an "
            "allocation. Let’s revalidate the mandate before we continue."
        ),
        "asset_allocation_analysis_stale": (
            "The stored allocation no longer matches the current signed assessment, so I "
            "can’t reuse those numbers. Let’s revalidate the mandate and run a fresh allocation."
        ),
        "signed_assessment_not_found": (
            "I couldn’t find the exact signed assessment you referenced, so I didn’t "
            "substitute another one or run an allocation."
        ),
        "asset_allocation_analysis_not_found": (
            "I don’t have a validated allocation result on file for this conversation yet. "
            "We’ll need a fresh allocation before I can answer quantitative follow-ups."
        ),
        "liquidity_requirement_unsupported": (
            "The signed liquidity requirement isn’t supported for allocation right now, so "
            "no allocation was accepted. The mandate needs a supported liquidity setup."
        ),
        "unsupported_constraint": (
            "I could not enforce a signed allocation constraint, so I have not created "
            "the proposal. No allocation, proposal, or policy was accepted or saved."
        ),
        "tool_execution_timeout": (
            "The allocation analysis took too long, so I didn’t accept a quantitative result. "
            "You can retry with the same signed assessment."
        ),
    }
    for result in reversed(list(tool_results)):
        if not isinstance(result, dict):
            continue
        if result.get("tool") not in {
            "run_asset_allocation",
            "get_asset_allocation_analysis",
        }:
            continue
        error = str(result.get("error") or "").strip()
        if error in messages:
            return messages[error]
        evidence = result.get("recommendation_evidence")
        if isinstance(evidence, dict) and evidence.get("valid_for_reporting") is not True:
            return (
                "I cannot provide a validated allocation from this run. "
                "No proposal or policy was accepted or saved."
            )
    return None

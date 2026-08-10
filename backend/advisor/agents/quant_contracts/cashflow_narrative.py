from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from advisor.agents.quant_contracts._shared import _finite_numeric, _normalize_visible_text
from advisor.agents.quant_contracts.claim_rendering import _format_claim_value
from advisor.agents.quant_contracts.models import QuantEvidenceEnvelope


def _render_cashflow_single_path_client_summary(
    envelope: QuantEvidenceEnvelope,
) -> Optional[str]:
    """Short plain-language summary for one-path cash-flow reporting fallbacks."""

    if (
        envelope.tool != "run_cashflow_projection"
        or _cashflow_evidence_sample_count(envelope) != 1
    ):
        return None
    claims = {claim.metric_key: claim for claim in envelope.claims}
    paragraphs: List[str] = [
        "Here's a baseline cash-flow check from your saved figures. "
        "This is one modeled path, not a range of possible market outcomes."
    ]
    facts: List[str] = []

    success_claim = claims.get("success_probability")
    success_value = (
        _finite_numeric(success_claim.value) if success_claim is not None else None
    )
    if success_claim is not None and success_value is not None:
        outcome = "passed" if success_value >= 1.0 else "did not pass"
        facts.append(
            f"On this path, the plan {outcome} the model's net-worth success check "
            f"[evidence: {envelope.tool}/{success_claim.claim_id}]"
        )

    terminal_claim = claims.get("projected_terminal_value")
    terminal_display = (
        _format_claim_value(terminal_claim.value, terminal_claim.unit)
        if terminal_claim is not None
        else None
    )
    if terminal_claim is not None and terminal_display:
        facts.append(
            f"Ending net worth is {terminal_display} "
            f"[evidence: {envelope.tool}/{terminal_claim.claim_id}]"
        )

    shortfall_claim = claims.get("shortfall")
    shortfall_value = (
        _finite_numeric(shortfall_claim.value) if shortfall_claim is not None else None
    )
    shortfall_display = (
        _format_claim_value(shortfall_claim.value, shortfall_claim.unit)
        if shortfall_claim is not None
        else None
    )
    if (
        shortfall_claim is not None
        and shortfall_display
        and shortfall_value is not None
        and shortfall_value > 0
    ):
        facts.append(
            f"Projected shortfall at the end of the horizon is {shortfall_display} "
            f"[evidence: {envelope.tool}/{shortfall_claim.claim_id}]"
        )

    depletion_claim = claims.get("first_depletion_year_distribution")
    if depletion_claim is not None and isinstance(depletion_claim.value, dict):
        probabilities = depletion_claim.value.get("probability_by_year")
        nonzero_years = (
            sorted(
                str(year)
                for year, probability in probabilities.items()
                if _finite_numeric(probability) is not None
                and _finite_numeric(probability) > 0
            )
            if isinstance(probabilities, dict)
            else []
        )
        if nonzero_years:
            facts.append(
                f"First modeled net-worth depletion year is {nonzero_years[0]} "
                f"[evidence: {envelope.tool}/{depletion_claim.claim_id}]"
            )

    if not facts:
        return None
    paragraphs.append(" ".join(f"{item}." for item in facts))
    paragraphs.append(
        "This uses standard model defaults for growth, taxes, and inflation. "
        "If you want, I can walk through the key inputs or run a range of market "
        "outcomes next."
    )
    joined = "\n\n".join(paragraphs)
    for warning in envelope.warnings:
        warning_text = " ".join(str(warning).split()).strip()
        if (
            warning_text
            and _normalize_visible_text(warning_text)
            not in _normalize_visible_text(joined)
        ):
            paragraphs.append(warning_text)
            joined = "\n\n".join(paragraphs)
    return joined


def _render_cashflow_evidence_interpretation(
    envelope: QuantEvidenceEnvelope,
    *,
    deterministic_single_path: bool = False,
) -> List[str]:
    """Explain relationships among typed cash-flow claims without inventing values."""

    claims = {claim.metric_key: claim for claim in envelope.claims}
    rendered: List[str] = []

    if deterministic_single_path:
        terminal_claim = claims.get("projected_terminal_value")
        terminal_value = (
            _finite_numeric(terminal_claim.value)
            if terminal_claim is not None
            else None
        )
        shortfall_claim = claims.get("shortfall")
        shortfall_value = (
            _finite_numeric(shortfall_claim.value)
            if shortfall_claim is not None
            else None
        )
        if (
            terminal_claim is not None
            and terminal_value is not None
            and terminal_value < 0
            and shortfall_claim is not None
            and shortfall_value is not None
            and shortfall_value > 0
        ):
            rendered.append(
                "The negative ending net worth and positive ending shortfall describe "
                "the same deteriorating baseline trajectory. The shortfall is cumulative "
                "modeled unmet cash flow at the end of the horizon, not an amount the "
                "client needs to deposit today "
                f"[evidence: {envelope.tool}/{terminal_claim.claim_id}; "
                f"{envelope.tool}/{shortfall_claim.claim_id}]"
            )
        liquidity_claim = claims.get("minimum_liquidity")
        minimum_liquidity = (
            _finite_numeric(liquidity_claim.value)
            if liquidity_claim is not None
            else None
        )
        if (
            liquidity_claim is not None
            and minimum_liquidity is not None
            and minimum_liquidity <= 0
        ):
            rendered.append(
                "Liquid cash reaches the model floor on this path, so investment "
                "balances alone should not be read as continuous cash-flow coverage "
                f"[evidence: {envelope.tool}/{liquidity_claim.claim_id}]"
            )
        rendered.append(
            "A one-path baseline cannot quantify outcome likelihood or market-risk "
            "dispersion. Use a Monte Carlo scenario for that question after verifying "
            "the planning inputs."
        )
        return rendered

    terminal_claim = claims.get("terminal_value_percentiles")
    terminal = terminal_claim.value if terminal_claim and isinstance(terminal_claim.value, dict) else {}
    terminal_p10 = _finite_numeric(terminal.get("p10"))
    terminal_p50 = _finite_numeric(terminal.get("p50"))
    if (
        terminal_claim is not None
        and terminal_p10 is not None
        and terminal_p50 is not None
        and terminal_p10 < 0 <= terminal_p50
    ):
        rendered.append(
            "Downside dispersion is important: lower-decile terminal net worth is negative "
            "while median terminal net worth is nonnegative, so the median does not describe "
            "the lower-tail paths "
            f"[evidence: {envelope.tool}/{terminal_claim.claim_id}]"
        )

    shortfall_claim = claims.get("shortfall_percentiles")
    shortfall = (
        shortfall_claim.value
        if shortfall_claim and isinstance(shortfall_claim.value, dict)
        else {}
    )
    shortfall_p10 = _finite_numeric(shortfall.get("p10"))
    shortfall_p50 = _finite_numeric(shortfall.get("p50"))
    if (
        shortfall_claim is not None
        and shortfall_p10 is not None
        and shortfall_p50 is not None
        and shortfall_p10 <= 0 < shortfall_p50
    ):
        rendered.append(
            "Cash-flow outcomes also vary across paths: the lower-decile terminal shortfall "
            "is absent while the median terminal shortfall is positive "
            f"[evidence: {envelope.tool}/{shortfall_claim.claim_id}]"
        )

    liquidity_claim = claims.get("minimum_liquidity")
    minimum_liquidity = (
        _finite_numeric(liquidity_claim.value) if liquidity_claim is not None else None
    )
    if liquidity_claim is not None and minimum_liquidity is not None and minimum_liquidity <= 0:
        rendered.append(
            "Liquidity reaches the model floor in the reported path statistic; nonnegative "
            "total net worth therefore should not be interpreted as continuous liquid-cash "
            "coverage "
            f"[evidence: {envelope.tool}/{liquidity_claim.claim_id}]"
        )

    if (
        shortfall_claim is not None
        and shortfall_p50 is not None
        and shortfall_p50 > 0
        and liquidity_claim is not None
        and minimum_liquidity is not None
        and minimum_liquidity <= 0
    ):
        rendered.append(
            "Investment-capacity implication: this run does not establish a supported "
            "additional monthly investment amount, because median terminal shortfall debt is "
            "positive and the reported liquidity statistic reaches the model floor "
            f"[evidence: {envelope.tool}/{shortfall_claim.claim_id}; "
            f"{envelope.tool}/{liquidity_claim.claim_id}]"
        )

    if claims.get("success_probability") and (
        claims.get("first_shortfall_year_distribution") or shortfall_claim
    ):
        success_claim = claims["success_probability"]
        related_claim = claims.get("first_shortfall_year_distribution") or shortfall_claim
        rendered.append(
            "The success and shortfall measures answer different questions: success tests "
            "total Net Worth, while shortfall debt tracks unmet cash flow; both should be "
            "reviewed together "
            f"[evidence: {envelope.tool}/{success_claim.claim_id}; "
            f"{envelope.tool}/{related_claim.claim_id}]"
        )

    distribution_claims = [
        claim
        for key in (
            "first_depletion_year_distribution",
            "first_shortfall_year_distribution",
        )
        if (claim := claims.get(key)) is not None and isinstance(claim.value, dict)
    ]
    sample_counts = {
        int(sample_count)
        for claim in distribution_claims
        if (sample_count := _finite_numeric(claim.value.get("sample_count"))) is not None
        and sample_count > 1
        and sample_count.is_integer()
    }
    if len(sample_counts) == 1:
        sample_count = next(iter(sample_counts))
        references = "; ".join(
            f"{envelope.tool}/{claim.claim_id}" for claim in distribution_claims
        )
        rendered.append(
            f"Sampling limitation: the probabilities and percentiles come from {sample_count:,} "
            "modeled paths. They are simulation estimates, not guarantees; a higher-path rerun "
            "can test whether the reported pattern is stable "
            f"[evidence: {references}]"
        )

    return rendered


def _cashflow_evidence_sample_count(
    envelope: QuantEvidenceEnvelope,
) -> Optional[int]:
    sample_counts: set[int] = set()
    for claim in envelope.claims:
        if claim.metric_key not in {
            "first_depletion_year_distribution",
            "first_shortfall_year_distribution",
        } or not isinstance(claim.value, dict):
            continue
        sample_count = _finite_numeric(claim.value.get("sample_count"))
        if (
            sample_count is not None
            and sample_count > 0
            and sample_count.is_integer()
        ):
            sample_counts.add(int(sample_count))
    return next(iter(sample_counts)) if len(sample_counts) == 1 else None


def _has_saved_cashflow_analysis(
    tool_results: Sequence[Dict[str, Any]],
) -> bool:
    for result in reversed(list(tool_results)):
        if not isinstance(result, dict) or result.get("tool") not in {
            "run_cashflow_projection",
            "get_cashflow_analysis",
        }:
            continue
        if result.get("tool") == "get_cashflow_analysis":
            return result.get("retrieved_without_rerun") is True
        persistence = (
            result.get("analysis_persistence")
            if isinstance(result.get("analysis_persistence"), dict)
            else {}
        )
        if str(result.get("analysis_id") or "").strip() and persistence.get("stored") is True:
            return True
    return False


def _select_cashflow_assumptions(assumptions: Sequence[str]) -> List[str]:
    """Keep the client-facing assumption section focused on decision-sensitive inputs."""

    cleaned = [str(item).strip() for item in assumptions if str(item).strip()]
    if len(cleaned) <= 6:
        return cleaned
    priority_markers = (
        "exact confirmed target allocations",
        "monte carlo configuration",
        "projection end age",
        "life expectancy",
        "income growth",
        "spending growth",
        "state tax jurisdiction",
        "tax filing status",
        "projection start year",
    )
    ranked = sorted(
        enumerate(cleaned),
        key=lambda item: (
            next(
                (
                    index
                    for index, marker in enumerate(priority_markers)
                    if marker in item[1].lower()
                ),
                len(priority_markers),
            ),
            item[0],
        ),
    )
    return [item for _, item in ranked[:6]]


def _has_saved_asset_allocation_analysis(
    tool_results: Sequence[Dict[str, Any]],
) -> bool:
    for result in reversed(list(tool_results)):
        if not isinstance(result, dict) or result.get("tool") not in {
            "run_asset_allocation",
            "get_asset_allocation_analysis",
        }:
            continue
        if result.get("tool") == "get_asset_allocation_analysis":
            return result.get("retrieved_without_rerun") is True
        persistence = (
            result.get("analysis_persistence")
            if isinstance(result.get("analysis_persistence"), dict)
            else {}
        )
        if str(result.get("analysis_id") or "").strip() and persistence.get("stored") is True:
            return True
    return False


def _tool_display_name(tool_name: str) -> str:
    return {
        "run_cashflow_projection": "Cash-flow model",
        "get_cashflow_analysis": "Stored cash-flow analysis",
        "audit_cashflow_analysis": "Cash-flow calculation audit",
        "run_asset_allocation": "Asset-allocation model",
        "get_asset_allocation_analysis": "Stored asset-allocation analysis",
        "solve_cashflow_contribution": "Cash-flow contribution solver",
        "compare_quant_analyses": "Quantitative analysis comparison",
        "calculate_cashflow_metrics": "Stored cash-flow calculation",
        "calculate_financial_math": "Financial-math calculation",
        "query_wolfram_alpha": "Wolfram|Alpha pure-math calculation",
        "analyze_portfolio_risk": "Portfolio risk analysis",
        "analyze_asset_location": "Asset-location analysis",
        "estimateAllocationRiskReturn": "Allocation risk/return estimate",
        "lookupRiskReturnFrontier": "Risk/return frontier lookup",
    }.get(tool_name, tool_name)

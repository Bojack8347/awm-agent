from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

from advisor.agents.quant_contracts._shared import (
    _finite_numeric,
    _normalize_visible_text,
    _numeric_leaves,
)
from advisor.agents.quant_contracts.evidence import _evidence_from_tool_results
from advisor.agents.quant_contracts.models import (
    QuantConclusionValidation,
    QuantEvidenceClaim,
    QuantEvidenceEnvelope,
    QuantValidationIssue,
)
from advisor.agents.quant_contracts.policy import quant_recommendations_enabled


_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<open>\()?(?P<sign_before>[-−])?"
    r"(?P<currency>\$)?(?P<sign_after>[-−])?(?P<number>\d[\d,]*(?:\.\d+)?)"
    r"(?P<suffix>[kKmMbB])?(?P<percent>%)?(?P<close>\))?(?![A-Za-z0-9_])"
)

_UUID_RE = re.compile(
    r"(?<![A-Fa-f0-9])[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-"
    r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}(?![A-Fa-f0-9])"
)


_ABSTENTION_RE = re.compile(
    r"\b(?:(?:i|we) (?:cannot|can't|could not|am unable to|are unable to) "
    r"(?:answer|calculate|confirm|conclude|determine|estimate|give|guarantee|provide|recommend|"
    r"report|run|say|state|support|verify)|"
    r"(?:not enough|insufficient|unavailable|unsupported|invalid) "
    r"(?:client data|data|evidence|inputs?|model evidence|model output|results?)|"
    r"(?:available )?(?:model )?result did not pass|"
    r"need (?:more|additional) (?:data|evidence|information|inputs?)|"
    r"before (?:i|we) can (?:answer|calculate|confirm|conclude|determine|estimate|"
    r"give|guarantee|provide|recommend|report|run|say|state|support|verify))\b",
    re.IGNORECASE,
)


_AFFIRMATIVE_GUARANTEE_RE = re.compile(
    r"\b(?:(?:portfolio|allocation|investment|model|result) "
    r"(?:is |are )?guarantee(?:d|s) (?:to )?(?:earn|produce|return)|"
    r"(?:provides? |offers? )?(?:a )?guaranteed (?:annual )?return|"
    r"guarantee(?:d|s) (?:a |the )?[^.!?]{0,24}\b(?:return|outcome)|"
    r"(?:cannot|can't|will not|won't|never) lose (?:money|value)|"
    r"risk[- ]free return)\b",
    re.IGNORECASE,
)


_GUARANTEE_REFUSAL_RE = re.compile(
    r"\b(?:(?:i|we|the model|this result) (?:cannot|can't|does not|doesn't) "
    r"(?:provide |make |support )?(?:a )?guarantee|"
    r"(?:no|not) (?:return|outcome) (?:is |can be )?guaranteed|"
    r"cannot be guaranteed)\b",
    re.IGNORECASE,
)


_RECOMMENDATION_RE = re.compile(
    r"\b(?:recommend(?:ed|ation|ing)?.{0,40}(?:invest|allocat|portfolio|retir|spend|"
    r"withdraw|proposal|policy|proceed)|(?:invest|allocat|portfolio|retir|spend|withdraw).{0,40}"
    r"recommend(?:ed|ation|ing)?|(?:i|we|you) (?:would|should) (?:invest|allocate|proceed|"
    r"retire|spend|withdraw)|(?:allocate|invest|put) (?:\$|\d)|safe to (?:invest|spend|"
    r"retire|proceed)|suitable|best interest|optimal portfolio|"
    r"proceed with (?:the )?allocation)\b",
    re.IGNORECASE,
)


_EVIDENCE_CONTEXT_RECOMMENDATION_RE = re.compile(
    r"\b(?:recommend(?:ed|ation|ing)?|should proceed|would proceed|go ahead|move forward)\b",
    re.IGNORECASE,
)


_QUANT_CONCLUSION_RE = re.compile(
    r"\b(?:feasible|can (?:afford|retire)|high (?:shortfall |depletion )?risk|material (?:shortfall |depletion )?risk|"
    r"significant (?:shortfall |depletion )?risk|(?:material|significant) (?:funding )?(?:gap|shortfall)|"
    r"depletion risk|sustainable|affordable|"
    r"on track|fully funded|meets? (?:the )?goal|plan (?:works|will work)|"
    r"(?:plan|position|outlook) (?:appears |looks |seems |is )?(?:robust|strong|healthy)|"
    r"(?:plenty|ample|strong|healthy) (?:of )?(?:financial )?capacity|"
    r"odds (?:appear |look |seem |are )?(?:strong|good|favorable|high)|"
    r"well[- ]positioned|healthy cushion|comfortably (?:afford|fund|sustain)|"
    r"retirement ready|likely to succeed|risk target (?:was |is )?(?:met|achieved)|"
    r"passive[- ]only)\b",
    re.IGNORECASE,
)


_UNSUPPORTED_MONTHLY_CASHFLOW_SCENARIO_RE = re.compile(
    r"\b(?:test|simulate)\b.{0,100}\b(?:monthly (?:investment|contribution|amount)|"
    r"invest(?:ing|ment)? .{0,30}(?:monthly|per month))\b",
    re.IGNORECASE,
)


_QUANT_NUMERIC_CONTEXT_RE = re.compile(
    r"\b(?:model(?:ed|led)?|simulation|projection|cash[- ]?flow|portfolio|"
    r"success probability|shortfall|funding gap|ending balance|terminal value|"
    r"expected return|volatility|depletion|reserve breach|allocation)\b",
    re.IGNORECASE,
)


_DIRECT_FACT_CONTEXT_RE = re.compile(
    r"\b(?:current age|age|aged|retire(?:ment)?(?: at| through)?|life expectancy|"
    r"annual (?:income|spending|expense|contribution)|salary|account balance|"
    r"cash balance|brokerage balance|retirement balance|retirement account|"
    r"money pool|taxable pool|contribution|time horizon)\b",
    re.IGNORECASE,
)


_EXCLUSION_CLAIM_RE = re.compile(
    r"\b(?:exclusion(?:s)? (?:was|were|is|are) (?:honored|met|applied)|"
    r"excluded assets? (?:is|are|were) (?:absent|omitted|excluded)|"
    r"(?:bitcoin|crypto|hedge funds?) (?:is|are|was|were) (?:absent|omitted|excluded))\b",
    re.IGNORECASE,
)


_RECONCILIATION_CLAIM_RE = re.compile(
    r"\b(?:fully reconciled|reconciles? to|weights? sum to|dollars? sum to|complete implementation)\b",
    re.IGNORECASE,
)


_TARGET_RISK_CLAIM_RE = re.compile(
    r"\b(?:risk target|target volatility).{0,24}\b(?:met|achieved|satisfied|within tolerance)\b",
    re.IGNORECASE,
)


_CROSS_MODEL_LINEAGE_REQUEST_RE = re.compile(
    r"\b(?:which|what).{0,40}\ballocation.{0,50}\b(?:fed|linked|used)\b|"
    r"\b(?:life\s*model|lifemodel).{0,80}\bexpected return\b.{0,40}\b(?:direct|directly)\b",
    re.IGNORECASE,
)


_AFFIRMATIVE_DIRECT_OPTIMIZER_RETURN_RE = re.compile(
    r"\b(?:life\s*model|lifemodel)\b.{0,180}"
    r"\b(?:used|uses|received|receives|applied|applies|was permitted to use)\b.{0,180}"
    r"\b(?:optimizer(?:'s|’s)? expected return|expected[- ]return scalar)\b.{0,80}"
    r"\b(?:direct|directly)\b",
    re.IGNORECASE,
)


_NEGATED_DIRECT_OPTIMIZER_RETURN_RE = re.compile(
    r"\b(?:life\s*model|lifemodel)\b.{0,100}"
    r"\b(?:did not|does not|do not|never|was not permitted to)\b.{0,180}"
    r"\b(?:optimizer(?:'s|’s)? expected return|expected[- ]return scalar)\b",
    re.IGNORECASE,
)


_METRIC_CONTEXT_RULES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"\b(?:reserve|cash)[- ]?breach\b", re.IGNORECASE), ("reserve_breach",)),
    (re.compile(r"\bsuccess (?:probability|rate|odds)\b", re.IGNORECASE), ("success",)),
    (
        re.compile(
            r"\b(?:projected )?shortfall(?: (?:probability|risk|amount|debt))?\b",
            re.IGNORECASE,
        ),
        ("shortfall",),
    ),
    (re.compile(r"\b(?:education |funding )?gap\b", re.IGNORECASE), ("gap",)),
    (re.compile(r"\bexpected return\b", re.IGNORECASE), ("expected_return",)),
    (re.compile(r"\b(?:expected |target )?volatility\b", re.IGNORECASE), ("volatility",)),
    (re.compile(r"\b(?:liquid |net[- ]worth )?depletion\b", re.IGNORECASE), ("depletion",)),
    (
        re.compile(r"\b(?:ending balance|terminal (?:value|balance|net worth))\b", re.IGNORECASE),
        ("ending", "terminal"),
    ),
)


def validate_quantitative_response(
    response_text: str,
    tool_results: Sequence[Dict[str, Any]],
    *,
    client_file: Optional[Dict[str, Any]] = None,
    user_message: str = "",
    recommendation_enabled: Optional[bool] = None,
) -> QuantConclusionValidation:
    """Validate the final client-facing text against deterministic evidence."""

    evidence = _evidence_from_tool_results(tool_results)
    tools_seen = [item.tool for item in evidence]
    policy_enabled = (
        quant_recommendations_enabled()
        if recommendation_enabled is None
        else bool(recommendation_enabled)
    )
    text = " ".join(str(response_text or "").split())
    # Deterministic warning text is disclosure, not an agent-authored conclusion.
    # Keep it in ``text`` for exact-warning verification, but remove it from the
    # claim classifier so warnings such as "recommendation policy not supplied"
    # cannot turn a factual estimate into a recommendation claim.
    claim_text = _without_visible_warnings(text, evidence)
    recommendation_claim = bool(_RECOMMENDATION_RE.search(claim_text)) or bool(
        evidence and _EVIDENCE_CONTEXT_RECOMMENDATION_RE.search(claim_text)
    )
    conclusion_claim = bool(_QUANT_CONCLUSION_RE.search(claim_text))
    exclusion_claim = bool(_EXCLUSION_CLAIM_RE.search(claim_text))
    reconciliation_claim = bool(_RECONCILIATION_CLAIM_RE.search(claim_text))
    target_risk_claim = bool(_TARGET_RISK_CLAIM_RE.search(claim_text))
    guarantee_claim = bool(_AFFIRMATIVE_GUARANTEE_RE.search(claim_text)) and not bool(
        _GUARANTEE_REFUSAL_RE.search(claim_text)
    )
    positive_claim = bool(
        recommendation_claim
        or conclusion_claim
        or exclusion_claim
        or reconciliation_claim
        or target_risk_claim
        or guarantee_claim
    )
    # An abstention is safe only when the same response does not also make the
    # positive claim it purports to withhold ("metrics are missing, but proceed").
    abstains = bool(_ABSTENTION_RE.search(claim_text)) and not positive_claim
    numeric_claims = _extract_response_numbers(claim_text)
    quantitative_claim = bool(
        recommendation_claim
        or conclusion_claim
        or exclusion_claim
        or reconciliation_claim
        or target_risk_claim
        or guarantee_claim
        or (numeric_claims and evidence)
        or (numeric_claims and _QUANT_NUMERIC_CONTEXT_RE.search(claim_text))
    )

    if not evidence and not quantitative_claim:
        return QuantConclusionValidation(
            status="not_applicable",
            valid_for_recommendation=False,
            recommendation_policy_enabled=policy_enabled,
        )

    issues: List[QuantValidationIssue] = []
    nonreportable_evidence = [item for item in evidence if not item.valid_for_reporting]
    conclusion_ineligible_evidence = [item for item in evidence if not item.valid_for_conclusion]
    recommendation_ineligible_evidence = [item for item in evidence if not item.valid_for_recommendation]
    if nonreportable_evidence and quantitative_claim and not abstains:
        issues.append(
            QuantValidationIssue(
                type="invalid_quant_evidence",
                detail=(
                    "A quantitative claim relied on a result that was not safe even for factual "
                    "reporting because it was invalid, incomplete, or corrupt."
                ),
            )
        )
    elif recommendation_ineligible_evidence and recommendation_claim and not abstains:
        issues.append(
            QuantValidationIssue(
                type="quant_evidence_not_recommendation_grade",
                detail=(
                    "The result may be reported as a labeled estimate, but it cannot support a "
                    "recommendation."
                ),
            )
        )
    elif (
        conclusion_ineligible_evidence
        and (conclusion_claim or exclusion_claim or reconciliation_claim or target_risk_claim)
        and not abstains
    ):
        issues.append(
            QuantValidationIssue(
                type="quant_evidence_not_conclusion_grade",
                detail=(
                    "The result may be reported as a labeled estimate, but it cannot support a "
                    "feasibility, risk-category, or constraint conclusion."
                ),
            )
        )
    if not evidence and quantitative_claim:
        issues.append(
            QuantValidationIssue(
                type="missing_quant_evidence",
                detail="No deterministic quantitative evidence supports the client-facing claim.",
            )
        )
    if guarantee_claim:
        issues.append(
            QuantValidationIssue(
                type="unsupported_guarantee_claim",
                detail=(
                    "A modeled allocation cannot guarantee returns or the absence of loss."
                ),
            )
        )
    if (
        _CROSS_MODEL_LINEAGE_REQUEST_RE.search(str(user_message or ""))
        and not any(
            item.tool in {"run_cashflow_projection", "get_cashflow_analysis"}
            for item in evidence
        )
        and not abstains
    ):
        issues.append(
            QuantValidationIssue(
                type="missing_cross_model_lineage_evidence",
                detail=(
                    "A model-interaction answer requires the linked cash-flow analysis; "
                    "allocation evidence alone cannot establish what LifeModel consumed."
                ),
            )
        )
    if (
        _AFFIRMATIVE_DIRECT_OPTIMIZER_RETURN_RE.search(claim_text)
        and not _NEGATED_DIRECT_OPTIMIZER_RETURN_RE.search(claim_text)
    ):
        issues.append(
            QuantValidationIssue(
                type="invalid_cross_model_method_claim",
                detail=(
                    "The AWM bridge supplies allocation weights to LifeModel. LifeModel applies "
                    "its own configured per-asset return, volatility, and correlation assumptions; "
                    "it does not consume the optimizer's expected-return scalar directly."
                ),
            )
        )

    allowed_numbers = _allowed_numeric_values(
        evidence,
        client_file=client_file or {},
        user_message=user_message,
    )
    numeric_search_start = 0
    if evidence and not abstains:
        for display, value, is_percent in numeric_claims:
            display_start = claim_text.find(display, numeric_search_start)
            if display_start < 0:
                display_start = claim_text.find(display)
            if display_start >= 0:
                numeric_search_start = display_start + len(display)
            context = _numeric_display_context(
                claim_text,
                display,
                display_start=display_start,
            )
            if is_percent:
                evidence_supported = (
                    _number_is_supported_by_typed_evidence(
                        value,
                        is_percent=True,
                        evidence=evidence,
                        display=display,
                    )
                    or _percent_number_is_supported_by_evidence_text(
                        value,
                        evidence=evidence,
                        display=display,
                    )
                )
            else:
                evidence_supported = _number_is_supported(
                    value,
                    is_percent=False,
                    allowed=allowed_numbers,
                    display=display,
                ) or _number_is_supported_by_typed_evidence(
                    value,
                    is_percent=False,
                    evidence=evidence,
                    display=display,
                )
            direct_fact_supported = _direct_fact_number_is_supported(
                value,
                is_percent=is_percent,
                context=_direct_numeric_display_context(
                    claim_text,
                    display,
                    display_start=display_start,
                ),
                client_file=client_file or {},
                user_message=user_message,
                display=display,
            )
            if not evidence_supported and not direct_fact_supported:
                issues.append(
                    QuantValidationIssue(
                        type="unsupported_numeric_claim",
                        detail="The number does not match validated tool evidence or a direct Client File/user fact.",
                        claim=display,
                    )
                )
                continue
            if direct_fact_supported and not evidence_supported:
                continue
            metric_context = _numeric_metric_context(
                claim_text,
                display,
                display_start=display_start,
            )
            required_fragments = {
                fragment
                for pattern, fragments in _METRIC_CONTEXT_RULES
                if pattern.search(metric_context)
                for fragment in fragments
            }
            if required_fragments:
                candidate_metrics = _matching_evidence_metric_keys(
                    value,
                    is_percent=is_percent,
                    evidence=evidence,
                    display=display,
                )
                agent_interpreted_calculation = any(
                    metric_key.startswith("calculate_financial_math/")
                    for metric_key in candidate_metrics
                )
                # The calculator already validates sources, units, and operations.
                # Its specialist owns the semantic label after checking that trace;
                # this layer only binds the narrated number to the typed output.
                if not agent_interpreted_calculation and not any(
                    any(fragment in metric_key.lower() for fragment in required_fragments)
                    for metric_key in candidate_metrics
                ):
                    issues.append(
                        QuantValidationIssue(
                            type="mismatched_quant_metric_claim",
                            detail=(
                                "The number exists in tool evidence, but not under the financial "
                                "metric named by the response."
                            ),
                            claim=display,
                        )
                    )

    valid_codes = {item.conclusion_code for item in evidence if item.valid_for_conclusion}
    lowered = claim_text.lower()
    if conclusion_claim and not abstains:
        affirmative_feasibility_text = re.sub(
            (
                r"\b(?:no|not|never|without|cannot|can't|could not|did not|"
                r"does not|failed to)\b[^.!?\n]{0,80}\bfeasible\b"
            ),
            "",
            lowered,
        )
        if re.search(
            r"\b(?:feasible|sustainable|affordable|can afford|can retire)\b",
            affirmative_feasibility_text,
        ):
            if not valid_codes.intersection(
                {
                    "feasible",
                    "feasible_with_tradeoffs",
                    "bounded_solution",
                    "search_ceiling_feasible",
                    "baseline_satisfies_target",
                }
            ):
                issues.append(
                    QuantValidationIssue(
                        type="unsupported_conclusion_claim",
                        detail="The claimed feasibility category is not present in validated evidence.",
                    )
                )
        if re.search(r"\b(?:high|material|significant).{0,20}risk\b|\bdepletion risk\b", lowered):
            if "high_shortfall_risk" not in valid_codes:
                issues.append(
                    QuantValidationIssue(
                        type="unsupported_conclusion_claim",
                        detail="The claimed risk category is not present in validated evidence.",
                    )
                )

    allocation_evidence = [item for item in evidence if item.tool == "run_asset_allocation"]
    cashflow_evidence = [item for item in evidence if item.tool == "run_cashflow_projection"]
    contribution_solver_evidence = [
        item
        for item in evidence
        if item.tool == "solve_cashflow_contribution"
        and item.valid_for_conclusion
    ]
    if (
        cashflow_evidence
        and not contribution_solver_evidence
        and _UNSUPPORTED_MONTHLY_CASHFLOW_SCENARIO_RE.search(claim_text)
    ):
        issues.append(
            QuantValidationIssue(
                type="unsupported_cashflow_scenario_claim",
                detail=(
                    "No validated monthly-contribution solver evidence is present, "
                    "so the response cannot promise that rerun."
                ),
            )
        )
    if exclusion_claim and not _constraint_passed(allocation_evidence, "hard_exclusions"):
        issues.append(
            QuantValidationIssue(
                type="unsupported_constraint_claim",
                detail="The response claimed exclusions were honored without a passed hard-exclusion check.",
            )
        )
    if "passive-only" in lowered or "passive only" in lowered:
        if not _constraint_passed(allocation_evidence, "active_risk") or _has_active_security(tool_results):
            issues.append(
                QuantValidationIssue(
                    type="unsupported_constraint_claim",
                    detail="The response claimed passive-only implementation without compatible validated holdings.",
                )
            )
    if target_risk_claim and not _constraint_passed(allocation_evidence, "target_volatility"):
        issues.append(
            QuantValidationIssue(
                type="unsupported_constraint_claim",
                detail="The response claimed the risk target passed without a validated tolerance check.",
            )
        )
    if reconciliation_claim and not all(
        _constraint_passed(allocation_evidence, name)
        for name in ("asset_weight_sum", "security_weight_sum", "security_dollar_sum")
    ):
        issues.append(
            QuantValidationIssue(
                type="unsupported_reconciliation_claim",
                detail="The response claimed reconciliation without passed weight and dollar checks.",
            )
        )

    evidence_warnings = [warning for item in evidence for warning in item.warnings]
    if quantitative_claim and not abstains:
        for warning in evidence_warnings:
            if _normalize_visible_text(warning) not in _normalize_visible_text(text):
                issues.append(
                    QuantValidationIssue(
                        type="unpropagated_quant_warning",
                        detail="A deterministic tool warning was not displayed in the client-facing response.",
                        claim=warning,
                    )
                )

    all_evidence_reportable = bool(evidence) and all(
        item.valid_for_reporting for item in evidence
    )
    all_evidence_conclusive = bool(evidence) and all(
        item.valid_for_conclusion for item in evidence
    )
    all_evidence_valid = bool(evidence) and all(item.valid_for_recommendation for item in evidence)
    if recommendation_claim and all_evidence_valid and not policy_enabled:
        issues.append(
            QuantValidationIssue(
                type="quant_recommendation_disabled",
                detail=(
                    "Recommendation narration remains disabled until the production acceptance gates "
                    "are explicitly enabled."
                ),
            )
        )

    if issues:
        return QuantConclusionValidation(
            status="blocked",
            valid_for_reporting=False,
            valid_for_conclusion=False,
            valid_for_recommendation=False,
            recommendation_policy_enabled=policy_enabled,
            quant_tools_seen=tools_seen,
            evidence=evidence,
            errors=_dedupe_issues(issues),
        )
    if not all_evidence_reportable or abstains:
        return QuantConclusionValidation(
            status="abstained",
            valid_for_reporting=all_evidence_reportable,
            valid_for_conclusion=False,
            valid_for_recommendation=False,
            recommendation_policy_enabled=policy_enabled,
            quant_tools_seen=tools_seen,
            evidence=evidence,
        )
    return QuantConclusionValidation(
        status="passed",
        valid_for_reporting=all_evidence_reportable,
        valid_for_conclusion=all_evidence_conclusive,
        valid_for_recommendation=bool(all_evidence_valid and policy_enabled),
        recommendation_policy_enabled=policy_enabled,
        quant_tools_seen=tools_seen,
        evidence=evidence,
    )


def _allowed_numeric_values(
    evidence: Sequence[QuantEvidenceEnvelope],
    *,
    client_file: Dict[str, Any],
    user_message: str,
) -> List[float]:
    # Once quantitative evidence is in scope, every client-facing number must
    # be traceable to a typed claim (or an exact deterministic warning).  Broad
    # admission of unrelated Client File/user-message numbers lets an age or
    # contribution amount masquerade as a model statistic. Direct facts are
    # admitted separately only when their local text has a recognized input-fact
    # label and no model-metric context.
    values: List[float] = []
    for envelope in evidence:
        for warning in envelope.warnings:
            values.extend(value for _display, value, _is_percent in _extract_response_numbers(warning))
        for assumption in envelope.assumptions:
            values.extend(
                value
                for _display, value, _is_percent in _extract_response_numbers(assumption)
            )
        if not envelope.valid_for_reporting:
            continue
        for claim in envelope.claims:
            values.extend(value for _path, value in _claim_numeric_values(claim))
    return values


def _extract_response_numbers(text: str) -> List[tuple[str, float, bool]]:
    output: List[tuple[str, float, bool]] = []
    source = str(text or "")
    identifier_ranges = [
        (match.start(), match.end())
        for match in _UUID_RE.finditer(source)
    ]
    for match in _NUMBER_RE.finditer(source):
        if any(
            start <= match.start() and match.end() <= end
            for start, end in identifier_ranges
        ):
            continue
        raw = match.group("number").replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        suffix = (match.group("suffix") or "").lower()
        trailing_context = source[
            match.end() : match.end() + 24
        ].lower()
        if (
            not match.group("currency")
            and raw == "401"
            and suffix == "k"
            and re.match(r"\s*(?:account|balance|contribution|plan|withdrawal)", trailing_context)
        ):
            # 401k is an account label, not 401 thousand.
            continue
        if (
            not match.group("currency")
            and raw == "529"
            and not suffix
            and re.match(r"\s*(?:account|balance|contribution|plan|withdrawal)", trailing_context)
        ):
            # 529 is a statutory account label, not a quantitative claim.
            continue
        if suffix:
            value *= {"k": 1_000.0, "m": 1_000_000.0, "b": 1_000_000_000.0}[suffix]
        start = match.start()
        preceding = source[max(0, start - 32) : start]
        negative_word = bool(re.search(r"\bnegative\s*$", preceding, re.IGNORECASE))
        # Parentheses normally denote a negative accounting value. In prose such
        # as "60% ($90,000) to US Equity", however, they group the dollar
        # equivalent of the immediately preceding weight and are not a sign.
        grouped_after_percentage = bool(
            re.search(r"\d(?:\.\d+)?%\s*$", preceding)
        )
        grouped_after_labeled_fact = bool(
            re.search(
                r"\b(?:account|amount|balance|money pool|pool)\s*$",
                preceding,
                re.IGNORECASE,
            )
        )
        grouped_after_labeled_metric = bool(
            re.search(
                r"\b(?:drawdown|probability|rate|return|value|volatility)\s*$",
                preceding,
                re.IGNORECASE,
            )
        )
        parenthesized = bool(
            match.group("open")
            and match.group("close")
            and not grouped_after_percentage
            and not grouped_after_labeled_fact
            and not grouped_after_labeled_metric
        )
        if match.group("sign_before") or match.group("sign_after") or negative_word or parenthesized:
            value = -abs(value)
        is_percent = bool(match.group("percent"))
        is_plain_small_integer = (
            not match.group("currency")
            and not suffix
            and not is_percent
            and value.is_integer()
            and abs(value) <= 10
        )
        if is_plain_small_integer:
            continue
        output.append((match.group(0), value, is_percent))
    return output


def _number_is_supported(
    value: float,
    *,
    is_percent: bool,
    allowed: Iterable[float],
    display: Optional[str] = None,
) -> bool:
    candidates = [value]
    if is_percent:
        candidates.append(value / 100.0)
    for source in allowed:
        if is_percent:
            # Accept the rounding implied by the displayed precision. This
            # preserves evidence matching when the advisor naturally renders
            # 0, 1, or 2 decimal places without admitting a materially different
            # percentage.
            if math.isclose(
                value,
                source * 100.0,
                rel_tol=0.0,
                abs_tol=_percent_display_tolerance(display),
            ):
                return True
        for candidate in candidates:
            tolerance = max(
                1e-8,
                abs(source) * 1e-6,
                _numeric_display_tolerance(display),
            )
            if math.isclose(candidate, source, rel_tol=1e-6, abs_tol=tolerance):
                return True
    return False


def _percent_display_tolerance(display: Optional[str]) -> float:
    if not display:
        return 0.005000001
    match = re.search(r"\d[\d,]*(?:\.(\d+))?\s*%", str(display))
    if match is None:
        return 0.005000001
    decimal_places = len(match.group(1) or "")
    return (0.5 * (10.0 ** -decimal_places)) + 1e-9


def _numeric_display_tolerance(display: Optional[str]) -> float:
    if not display or "%" in str(display):
        return 0.0
    match = re.search(
        r"(?:\$|\b)(?:\d[\d,]*)(?:\.(\d+))?",
        str(display),
    )
    if match is None:
        return 0.0
    decimal_places = len(match.group(1) or "")
    return (0.5 * (10.0 ** -decimal_places)) + 1e-9


def _number_is_supported_by_typed_evidence(
    value: float,
    *,
    is_percent: bool,
    evidence: Sequence[QuantEvidenceEnvelope],
    display: Optional[str] = None,
) -> bool:
    """Accept only the display rounding explicitly defined for a claim unit."""

    fractional_percent_units = {
        "annual_decimal",
        "annual_decimal_0_to_1",
        "decimal",
        "decimal_0_to_1",
        "decimal_change",
        "decimal_share",
        "drawdown_decimal_0_to_1",
        "drawdown_threshold_decimal_0_to_1",
        "one_period_return_decimal",
        "probability_0_to_1",
        "share_of_total_variance",
        "weight_0_to_1",
    }
    percentage_point_units = {"percent", "percentage", "percentage_points"}
    display_tolerances = {
        "basis_points": 0.050000001,
        "USD": 0.005000001,
        "count": 0.0,
    }
    for envelope in evidence:
        if not envelope.valid_for_reporting:
            continue
        for claim in envelope.claims:
            for path, source in _claim_numeric_values(claim):
                if is_percent:
                    if claim.unit in fractional_percent_units:
                        expected_display = source * 100.0
                    elif claim.unit in percentage_point_units:
                        expected_display = source
                    elif claim.unit == "probability_by_calendar_year" and (
                        (
                            ".probability_by_year." in path
                            and not path.endswith("#key")
                        )
                        or path.endswith(".probability_never")
                    ):
                        expected_display = source * 100.0
                    elif claim.unit == "unspecified":
                        if _number_is_supported(
                            value,
                            is_percent=True,
                            allowed=[source],
                            display=display,
                        ):
                            return True
                        continue
                    else:
                        continue
                    if math.isclose(
                        value,
                        expected_display,
                        rel_tol=0.0,
                        abs_tol=_percent_display_tolerance(display),
                    ):
                        return True
                    continue
                tolerance = display_tolerances.get(claim.unit)
                if tolerance is not None and math.isclose(
                    value,
                    source,
                    rel_tol=0.0,
                    abs_tol=tolerance,
                ):
                    return True
    return False


def _percent_number_is_supported_by_evidence_text(
    value: float,
    *,
    evidence: Sequence[QuantEvidenceEnvelope],
    display: Optional[str] = None,
) -> bool:
    """Match percentages written verbatim in deterministic warnings/assumptions."""

    for envelope in evidence:
        for text in (*envelope.warnings, *envelope.assumptions):
            for _source_display, source, is_percent in _extract_response_numbers(text):
                if is_percent and math.isclose(
                    value,
                    source,
                    rel_tol=0.0,
                    abs_tol=_percent_display_tolerance(display),
                ):
                    return True
    return False


def _numeric_display_context(
    text: str,
    display: str,
    *,
    radius: int = 100,
    display_start: Optional[int] = None,
) -> str:
    """Return the containing sentence, bounded again for unpunctuated prose."""

    source = str(text or "")
    start = display_start if display_start is not None else source.find(display)
    if start < 0:
        return source
    left_boundaries = [source.rfind(marker, 0, start) for marker in (". ", "! ", "? ", "; ")]
    sentence_start = max(left_boundaries) + 2
    right_candidates = [
        index
        for marker in (". ", "! ", "? ", "; ")
        if (index := source.find(marker, start + len(display))) >= 0
    ]
    sentence_end = min(right_candidates) + 1 if right_candidates else len(source)
    return source[
        max(sentence_start, start - radius) : min(sentence_end, start + len(display) + radius)
    ]


def _numeric_metric_context(
    text: str,
    display: str,
    *,
    display_start: Optional[int] = None,
) -> str:
    """Return the local clause that labels a numeric value.

    Metric labels in a neighboring comma- or conjunction-delimited clause must
    not relabel the current number (for example, an allocation weight as an
    expected return merely because both appear in one sentence).
    """

    source = str(text or "")
    start = display_start if display_start is not None else source.find(display)
    if start < 0:
        return source
    end = start + len(display)
    left_candidates = [
        source.rfind(marker, 0, start)
        for marker in (",", ";", ". ", "! ", "? ", " and ", " but ")
    ]
    left = max(left_candidates) if left_candidates else -1
    if left >= 0 and source[left : left + 5] in {" and ", " but "}:
        left += 5
    else:
        left += 1
    right_candidates = [
        index
        for marker in (",", ";", ". ", "! ", "? ", " and ", " but ")
        if (index := source.find(marker, end)) >= 0
    ]
    right = min(right_candidates) if right_candidates else len(source)
    return source[max(0, left):right]


def _direct_numeric_display_context(
    text: str,
    display: str,
    *,
    display_start: Optional[int] = None,
) -> str:
    """Use a tight, mostly preceding window for labels attached to input facts."""

    start = display_start if display_start is not None else str(text or "").find(display)
    if start < 0:
        return str(text or "")
    return str(text or "")[max(0, start - 48) : start + len(display) + 8]


def _matching_evidence_metric_keys(
    value: float,
    *,
    is_percent: bool,
    evidence: Sequence[QuantEvidenceEnvelope],
    display: Optional[str] = None,
) -> set[str]:
    """Return metric keys containing the exact numeric evidence for a display."""

    metric_keys: set[str] = set()
    for envelope in evidence:
        if not envelope.valid_for_reporting:
            continue
        for claim in envelope.claims:
            claim_values = [number for _path, number in _claim_numeric_values(claim)]
            typed_claim_supported = _number_is_supported_by_typed_evidence(
                value,
                is_percent=is_percent,
                evidence=[envelope.model_copy(update={"claims": [claim]})],
                display=display,
            )
            generic_claim_supported = (
                not is_percent
                and _number_is_supported(
                    value,
                    is_percent=False,
                    allowed=claim_values,
                    display=display,
                )
            )
            if typed_claim_supported or generic_claim_supported:
                metric_keys.update(
                    {
                        claim.metric_key,
                        claim.evidence_ref,
                        claim.source_path,
                        *claim.semantic_metric_keys,
                    }
                )
    return metric_keys


def _claim_numeric_values(claim: QuantEvidenceClaim) -> List[tuple[str, float]]:
    """Return claim numbers, including typed calendar-year distribution keys."""

    output = _numeric_leaves(claim.value)
    decimal_value = _finite_numeric(claim.value_decimal)
    if decimal_value is not None and not any(
        math.isclose(value, decimal_value, rel_tol=0.0, abs_tol=0.0)
        for _path, value in output
    ):
        output.append(("$.value_decimal", decimal_value))
    if not isinstance(claim.value, dict):
        return output
    if claim.metric_key.endswith("_year_distribution"):
        probability_by_year = claim.value.get("probability_by_year")
        if isinstance(probability_by_year, dict):
            for year in probability_by_year:
                numeric_year = _finite_numeric(year)
                if numeric_year is not None and numeric_year.is_integer():
                    output.append((f"$.probability_by_year.{year}#key", numeric_year))
    if (
        claim.metric_key.endswith("_percentile_trajectory")
        or claim.unit == "LifeModel_column_value_by_calendar_year_and_percentile"
    ):
        for year in claim.value:
            numeric_year = _finite_numeric(year)
            if numeric_year is not None and numeric_year.is_integer():
                output.append((f"$.{year}#key", numeric_year))
    return output


def _direct_fact_number_is_supported(
    value: float,
    *,
    is_percent: bool,
    context: str,
    client_file: Dict[str, Any],
    user_message: str,
    display: Optional[str] = None,
) -> bool:
    """Permit clearly labeled input facts without letting them impersonate model metrics."""

    if not _DIRECT_FACT_CONTEXT_RE.search(context):
        return False
    if _QUANT_NUMERIC_CONTEXT_RE.search(context):
        return False
    fact_values = [number for _path, number in _numeric_leaves(client_file)]
    fact_values.extend(
        number for _display, number, _is_percent in _extract_response_numbers(user_message)
    )
    return _number_is_supported(
        value,
        is_percent=is_percent,
        allowed=fact_values,
        display=display,
    )


def _constraint_passed(evidence: Sequence[QuantEvidenceEnvelope], name: str) -> bool:
    return any(item.constraint_checks.get(name, {}).get("passed") is True for item in evidence)


def _has_active_security(tool_results: Sequence[Dict[str, Any]]) -> bool:
    for result in tool_results:
        if not isinstance(result, dict) or result.get("tool") != "run_asset_allocation":
            continue
        full_result = result.get("full_result") if isinstance(result.get("full_result"), dict) else {}
        securities = full_result.get("securities") if isinstance(full_result.get("securities"), list) else []
        for security in securities:
            if not isinstance(security, dict):
                continue
            security_type = str(security.get("security_type") or "").strip().lower()
            if security_type and security_type != "passive":
                return True
    return False


def _without_visible_warnings(
    text: str,
    evidence: Sequence[QuantEvidenceEnvelope],
) -> str:
    output = str(text or "")
    # Deterministic disclosures are context, not agent-authored advice. Remove
    # their exact text before classifying the remaining narration.
    disclosures = (
        disclosure
        for item in evidence
        for disclosure in (*item.warnings, *item.assumptions)
    )
    for disclosure in disclosures:
        normalized_disclosure = " ".join(str(disclosure or "").split())
        if normalized_disclosure:
            output = re.sub(
                re.escape(normalized_disclosure),
                " ",
                output,
                flags=re.IGNORECASE,
            )
    return " ".join(output.split())


def _dedupe_issues(issues: Sequence[QuantValidationIssue]) -> List[QuantValidationIssue]:
    output: List[QuantValidationIssue] = []
    seen: set[tuple[str, Optional[str]]] = set()
    for issue in issues:
        key = (issue.type, issue.claim)
        if key in seen:
            continue
        seen.add(key)
        output.append(issue)
    return output

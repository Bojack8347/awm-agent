from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from advisor.agents.quant_contracts._shared import _finite_numeric, _normalize_visible_text
from advisor.agents.quant_contracts.claim_rendering import _render_evidence_claim
from advisor.agents.quant_contracts.cashflow_narrative import _select_cashflow_assumptions
from advisor.agents.quant_contracts.constants import QUANT_TOOL_NAMES
from advisor.agents.quant_contracts.evidence import _evidence_from_tool_results, build_quant_evidence
from advisor.agents.quant_contracts.models import QuantResponseAnnotations


_EVIDENCE_CITATION_RE = re.compile(
    r"\[evidence:\s*([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.\[\]-]+)"
    r"(?:\s*;\s*[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.\[\]-]+)*\s*\]",
    re.IGNORECASE,
)


_EVIDENCE_CITATION_ITEM_RE = re.compile(
    r"([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.\[\]-]+?)(?=\s*(?:;|\]))"
)


def build_quant_response_annotations(
    response_text: str,
    tool_results: Sequence[Dict[str, Any]],
) -> QuantResponseAnnotations:
    """Return a typed audit record for inline tool/claim references."""

    evidence = _evidence_from_tool_results(tool_results)
    if not evidence:
        return QuantResponseAnnotations(status="not_applicable")
    available = sorted(
        {
            f"{envelope.tool}/{claim.claim_id}"
            for envelope in evidence
            for claim in envelope.claims
        }
    )
    cited = []
    for block in re.findall(r"\[evidence:[^\]]+\]", str(response_text or ""), re.I):
        cited.extend(
            f"{tool}/{claim_id}"
            for tool, claim_id in _EVIDENCE_CITATION_ITEM_RE.findall(block)
        )
    cited = list(dict.fromkeys(cited))
    invalid = sorted(set(cited) - set(available))
    if invalid:
        status = "invalid"
    elif cited:
        status = "complete"
    else:
        status = "missing"
    return QuantResponseAnnotations(
        status=status,
        cited_claim_ids=cited,
        available_claim_ids=available,
        invalid_claim_ids=invalid,
    )


def attach_quant_evidence(tool_name: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Attach the typed evidence envelope before a tool result reaches an agent."""

    if tool_name not in QUANT_TOOL_NAMES or not isinstance(result, dict):
        return result
    output = dict(result)
    evidence = build_quant_evidence(tool_name, output)
    output["ok"] = evidence.valid_for_reporting
    output["execution_ok"] = evidence.execution_ok
    output["valid_for_reporting"] = evidence.valid_for_reporting
    output["valid_for_conclusion"] = evidence.valid_for_conclusion
    output["valid_for_recommendation"] = evidence.valid_for_recommendation
    output["permitted_use"] = evidence.permitted_use
    output["recommendation_evidence"] = evidence.model_dump(mode="json")
    return output


def visible_quant_warnings(tool_results: Sequence[Dict[str, Any]]) -> List[str]:
    """Return deterministic warnings that must be visible to the client."""

    warnings: List[str] = []
    seen: set[str] = set()
    for envelope in _evidence_from_tool_results(tool_results):
        # Adapter and transport failures belong in trace, not client chat.
        # Missing-input UX is rendered separately from typed missing_data.
        if not envelope.valid_for_reporting:
            continue
        for warning in envelope.warnings:
            normalized = _normalize_visible_text(warning)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            warnings.append(" ".join(warning.split()))
    return warnings


def visible_quant_assumptions(tool_results: Sequence[Dict[str, Any]]) -> List[str]:
    """Return resolved model assumptions that must accompany reported values."""

    assumptions: List[str] = []
    seen: set[str] = set()
    for envelope in _evidence_from_tool_results(tool_results):
        if not envelope.valid_for_reporting:
            continue
        selected_assumptions = (
            _select_cashflow_assumptions(envelope.assumptions)
            if envelope.tool
            in {"run_cashflow_projection", "get_cashflow_analysis"}
            else envelope.assumptions
        )
        for assumption in selected_assumptions:
            normalized = _normalize_visible_text(assumption)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            assumptions.append(" ".join(assumption.split()))
    return assumptions


def propagate_quant_warnings(
    response_text: str,
    tool_results: Sequence[Dict[str, Any]],
    *,
    include_assumptions: bool = True,
) -> str:
    """Append missing deterministic warnings and resolved assumptions before delivery."""

    text = str(response_text or "").strip()
    normalized_text = _normalize_visible_text(text)
    missing_warnings = [
        warning
        for warning in visible_quant_warnings(tool_results)
        if _normalize_visible_text(warning) not in normalized_text
    ]
    missing_assumptions = (
        [
            assumption
            for assumption in visible_quant_assumptions(tool_results)
            if _normalize_visible_text(assumption) not in normalized_text
        ]
        if include_assumptions
        else []
    )
    if not missing_warnings and not missing_assumptions:
        return text
    disclosures: List[str] = []
    if missing_assumptions:
        disclosures.append(
            "Applied model assumptions:\n"
            + "\n".join(
                f"- {assumption.rstrip('.')}."
                for assumption in missing_assumptions
            )
        )
    if missing_warnings:
        disclosures.append(
            "Model limitations:\n"
            + "\n".join(f"- {warning}" for warning in missing_warnings)
        )
    disclosure = "\n\n".join(disclosures)
    return f"{text}\n\n{disclosure}" if text else disclosure


def ensure_required_allocation_proposal_metrics(
    response_text: str,
    tool_results: Sequence[Dict[str, Any]],
) -> str:
    """Append required proposal metrics when model narration omits them."""

    allocation = next(
        (
            envelope
            for envelope in reversed(_evidence_from_tool_results(tool_results))
            if envelope.tool == "run_asset_allocation"
            and envelope.valid_for_recommendation
        ),
        None,
    )
    if allocation is None:
        return str(response_text or "").strip()

    text = str(response_text or "").strip()
    lowered = " ".join(text.lower().split())
    required = {
        "portfolio_expected_return_annual_decimal": "expected return",
        "portfolio_expected_volatility_annual_decimal": "expected volatility",
    }
    missing: List[str] = []
    claims = {claim.metric_key: claim for claim in allocation.claims}
    for metric_key, label in required.items():
        claim = claims.get(metric_key)
        value = _finite_numeric(claim.value) if claim is not None else None
        if claim is None or value is None:
            continue
        percent_variants = {
            f"{value * 100:g}%",
            f"{value * 100:.1f}%",
            f"{value * 100:.2f}%",
        }
        decimal_variants = {f"{value:g}", f"{value:.3f}", f"{value:.4f}"}
        label_present = label in lowered or (
            metric_key.endswith("volatility_annual_decimal")
            and "expected annual volatility" in lowered
        )
        value_present = any(item in lowered for item in percent_variants | decimal_variants)
        if label_present and value_present:
            continue
        rendered = _render_evidence_claim(allocation.tool, claim)
        if rendered:
            missing.append(rendered)
    if not missing:
        return text
    required_block = "Required proposal metrics:\n" + "\n".join(
        f"- {item}." for item in missing
    )
    return f"{text}\n\n{required_block}" if text else required_block


def format_quant_response_for_client(response_text: str) -> str:
    """Strip internal markup and engineer-facing dumps before client delivery.

    Validation must run on the tagged text first; call this only after guards pass.
    """

    text = str(response_text or "").strip()
    if not text:
        return text
    text = re.sub(r"\s*\[evidence:[^\]]+\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[life_model:[^\]]+\]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"Cash-flow output is estimate-only;\s*"
        r"no approved recommendation policy was supplied\.?",
        "This projection is an estimate, not a recommendation.",
        text,
        flags=re.IGNORECASE,
    )
    text = _replace_applied_assumptions_appendix_for_client(text)
    text = _clean_model_limitations_appendix_for_client(text)
    text = _dedupe_client_warning_sections(text)
    text = _drop_engineer_reporting_sections(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _dedupe_client_warning_sections(response_text: str) -> str:
    """Merge repeated client warning sections without dropping unique warnings."""

    text = str(response_text or "")
    marker = "Things to keep in mind:"
    if text.count(marker) < 2:
        return text

    pattern = re.compile(
        r"(?:\n{0,2})Things to keep in mind:\s*\n(?P<body>(?:\s*-\s*[^\n]+\n?)*)",
        flags=re.IGNORECASE,
    )
    warnings: List[str] = []
    seen: set[str] = set()
    first_start: Optional[int] = None
    stripped = text
    matches = list(pattern.finditer(text))
    for match in matches:
        if first_start is None:
            first_start = match.start()
        for raw_line in match.group("body").splitlines():
            warning = re.sub(r"^\s*-\s*", "", raw_line).strip()
            normalized = _normalize_visible_text(warning)
            if warning and normalized not in seen:
                seen.add(normalized)
                warnings.append(warning)
    for match in reversed(matches):
        stripped = stripped[: match.start()] + stripped[match.end() :]
    if first_start is None or not warnings:
        return stripped
    insertion = (
        "\n\nThings to keep in mind:\n"
        + "\n".join(f"- {warning}" for warning in warnings)
    )
    return stripped[:first_start].rstrip() + insertion + stripped[first_start:].lstrip()


def _replace_applied_assumptions_appendix_for_client(response_text: str) -> str:
    """Replace the long assumption dump with one plain-language sentence."""

    text = str(response_text or "")
    marker = "Applied model assumptions:"
    start = text.find(marker)
    if start < 0:
        return text
    prefix = text[:start].rstrip()
    tail = text[start + len(marker) :]
    next_section = _next_disclosure_section_offset(tail)
    remainder = tail[next_section:] if next_section is not None else ""
    if re.search(
        r"\b(?:standard|model) defaults?\b",
        prefix,
        flags=re.IGNORECASE,
    ):
        return f"{prefix}{remainder}".strip()
    disclosure = (
        "This estimate uses standard model defaults for growth, taxes, and inflation."
    )
    if prefix:
        return f"{prefix}\n\n{disclosure}{remainder}".strip()
    return f"{disclosure}{remainder}".strip()


def _clean_model_limitations_appendix_for_client(response_text: str) -> str:
    """Keep limitation warnings, but drop internal life_model codes."""

    text = str(response_text or "")
    marker = "Model limitations:"
    start = text.find(marker)
    if start < 0:
        return text
    prefix = text[:start].rstrip()
    tail = text[start + len(marker) :]
    next_section = _next_disclosure_section_offset(tail)
    body = tail if next_section is None else tail[:next_section]
    remainder = "" if next_section is None else tail[next_section:]
    cleaned: List[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        line = re.sub(r"\[life_model:[^\]]+\]\s*", "", line, flags=re.IGNORECASE)
        line = re.sub(
            r"\s*\(source:\s*life_model\.[^)]+\)",
            "",
            line,
            flags=re.IGNORECASE,
        )
        line = " ".join(line.split()).strip(" .")
        if line:
            cleaned.append(line)
    # Prefer folding short warnings into prose instead of an engineer heading.
    if not cleaned:
        return f"{prefix}{remainder}".strip()
    if len(cleaned) == 1 and len(cleaned[0]) <= 120:
        warning = cleaned[0]
        if _normalize_visible_text(warning) in _normalize_visible_text(prefix):
            return f"{prefix}{remainder}".strip()
        joined = f"{prefix}\n\n{warning}." if prefix else f"{warning}."
        return f"{joined}{remainder}".strip()
    bullets = "\n".join(f"- {item}." for item in cleaned)
    section = f"Things to keep in mind:\n{bullets}"
    joined = f"{prefix}\n\n{section}" if prefix else section
    return f"{joined}{remainder}".strip()


def _drop_engineer_reporting_sections(response_text: str) -> str:
    """Remove audit-oriented sections that should never reach the chat UI."""

    text = str(response_text or "")
    patterns = (
        r"\n+What the model shows:\n(?:- .+\n?)+",
        r"\n+How to read this:\n(?:- .+\n?)+",
        r"\n+Evidence interpretation:\n(?:- .+\n?)+",
        r"\n+Next analysis step:\n(?:- .+\n?)+",
        r"\n+Saved result:[^\n]+(?:\n(?![A-Z]).*)*",
        r"^Cash-flow baseline — deterministic estimate\n+",
        r"^Retirement projection — modeled range\n+",
    )
    for pattern in patterns:
        text = re.sub(pattern, "\n\n", text, flags=re.IGNORECASE | re.MULTILINE)
    return text.strip()


def _next_disclosure_section_offset(tail: str) -> Optional[int]:
    candidates = [
        index
        for section in (
            "\n\nModel limitations:",
            "\n\nThings to keep in mind:",
            "\n\nLimitations:",
            "\n\nWarnings:",
            "\n\nNext analysis step:",
            "\n\nSaved result:",
        )
        if (index := tail.find(section)) >= 0
    ]
    return min(candidates) if candidates else None

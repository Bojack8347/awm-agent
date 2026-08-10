from __future__ import annotations

from typing import Any, Dict, List, Optional

from advisor.agents.runtime.client_file_state import _assessment_payload_is_signed


def _investment_assessment_artifacts_from_tool_results(
    tool_results: List[Dict[str, Any]],
    response_text: str,
) -> List[Dict[str, Any]]:
    """Expose a drafted investment assessment as a UI sign-off card."""

    if any(
        isinstance(result, dict)
        and result.get("tool") == "record_assessment_signoff"
        and result.get("ok") is True
        for result in tool_results
    ):
        return []

    artifacts: List[Dict[str, Any]] = []
    for result in tool_results:
        if not isinstance(result, dict) or result.get("ok") is not True:
            continue
        tool_name = str(result.get("tool") or "")
        if tool_name == "create_investment_assessment":
            artifact = _investment_assessment_artifact_from_payload(
                result.get("payload")
                if isinstance(result.get("payload"), dict)
                else result.get("assessment")
                if isinstance(result.get("assessment"), dict)
                else {},
                response_text=response_text,
            )
            if artifact:
                artifacts.append(artifact)
            continue
    return artifacts


def _pending_unsigned_investment_assessments(
    client_file: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not isinstance(client_file, dict):
        return []
    pending: List[Dict[str, Any]] = []
    for key in ("investment_assessments", "signed_investment_assessments"):
        rows = client_file.get(key)
        if not isinstance(rows, list):
            continue
        for assessment in rows:
            if not isinstance(assessment, dict):
                continue
            if _assessment_payload_is_signed(assessment):
                continue
            status = str(
                assessment.get("assessment_status")
                or assessment.get("status")
                or ""
            ).strip().lower()
            if status not in {"pending", "pending_client_signoff"}:
                continue
            if status in {
                "signed_off",
                "approved",
                "confirmed",
                "declined",
                "rejected",
            }:
                continue
            if not (assessment.get("assessment_id") or assessment.get("id")):
                continue
            pending.append(assessment)
    return pending


def _investment_assessment_artifacts_from_client_file(
    client_file: Optional[Dict[str, Any]],
    response_text: str,
) -> List[Dict[str, Any]]:
    """Surface a durable pending assessment as an Agree card without re-running tools."""

    artifacts: List[Dict[str, Any]] = []
    for assessment in _pending_unsigned_investment_assessments(client_file):
        artifact = _investment_assessment_artifact_from_payload(
            assessment,
            response_text=response_text,
        )
        if artifact:
            artifacts.append(artifact)
    return artifacts


def _investment_assessment_artifact_from_payload(
    payload: Dict[str, Any],
    *,
    response_text: str,
) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict) or not payload:
        return None
    assessment = (
        payload.get("assessment")
        if isinstance(payload.get("assessment"), dict)
        else payload
        if payload.get("client_summary") or payload.get("assessment_id")
        else {}
    )
    if not isinstance(assessment, dict):
        assessment = {}
    summary = (
        assessment.get("client_summary")
        if isinstance(assessment.get("client_summary"), dict)
        else payload.get("client_summary")
        if isinstance(payload.get("client_summary"), dict)
        else {}
    )
    consultation_basis = (
        payload.get("consultation_basis")
        if isinstance(payload.get("consultation_basis"), dict)
        else assessment.get("consultation_basis")
        if isinstance(assessment.get("consultation_basis"), dict)
        else {}
    )
    paragraphs = [
        str(item).strip()
        for item in (summary.get("paragraphs") or [])
        if str(item).strip()
    ]
    assessment_id = str(
        payload.get("assessment_id")
        or assessment.get("assessment_id")
        or ""
    ).strip()
    try:
        assessment_version = int(
            payload.get("assessment_version")
            or assessment.get("assessment_version")
            or 0
        )
    except (TypeError, ValueError):
        assessment_version = 0
    investment_consultation_id = str(
        payload.get("investment_consultation_id")
        or assessment.get("investment_consultation_id")
        or consultation_basis.get("investment_consultation_id")
        or consultation_basis.get("consultation_id")
        or ""
    ).strip()
    money_pool_id = str(
        payload.get("money_pool_id")
        or assessment.get("money_pool_id")
        or ""
    ).strip()
    if not paragraphs:
        basis = (
            assessment.get("basis")
            if isinstance(assessment.get("basis"), dict)
            else payload.get("basis")
            if isinstance(payload.get("basis"), dict)
            else consultation_basis
        )
        paragraphs = _assessment_card_paragraphs(
            {
                **(basis if isinstance(basis, dict) else {}),
                "assessment_summary": str(
                    assessment.get("rationale")
                    or payload.get("rationale")
                    or ""
                ).strip(),
            },
            response_text,
        )
    if (
        not assessment_id
        or assessment_version <= 0
        or not investment_consultation_id
        or not money_pool_id
        or not paragraphs
    ):
        return None
    basis = (
        assessment.get("basis")
        if isinstance(assessment.get("basis"), dict)
        else {}
    )
    return {
        "artifact_type": "financial_planning_analysis",
        "payload": {
            "schema_version": "investment_assessment.v1",
            "artifact_type": "investment_assessment",
            "assessment_id": assessment_id,
            "assessment_version": assessment_version,
            "investment_consultation_id": investment_consultation_id,
            "money_pool_id": money_pool_id,
            "pool_label": str(
                basis.get("pool_label")
                or consultation_basis.get("pool_label")
                or "Investment Pool"
            ),
            "consultation_basis": consultation_basis,
            "client_summary": {
                "title": str(
                    summary.get("title") or "Investment Consultation Summary"
                ),
                "subtitle": str(summary.get("subtitle") or "For your review"),
                "paragraphs": paragraphs,
            },
        },
        "writeback_target": "client_file.plans",
    }


def _assessment_card_paragraphs(facts: Dict[str, Any], response_text: str) -> List[str]:
    summary = str(facts.get("assessment_summary") or "").strip()
    details: List[str] = []
    for label, key in (
        ("Amount", "amount"),
        ("Purpose", "purpose"),
        ("Source", "source_of_funds"),
        ("Horizon", "horizon_years"),
        ("Risk", "risk_tolerance"),
        ("Liquidity", "liquidity_needs"),
    ):
        value = facts.get(key)
        if value in (None, ""):
            continue
        if key == "amount":
            try:
                value = f"${float(value):,.0f}"
            except (TypeError, ValueError):
                value = str(value)
        elif key == "horizon_years":
            value = f"{value} years"
        details.append(f"{label}: {value}")
    paragraphs: List[str] = []
    if details:
        paragraphs.append("; ".join(details) + ".")
    if summary:
        paragraphs.append(summary)
    if not paragraphs:
        clean = str(response_text or "").strip()
        if clean:
            paragraphs.append(clean)
    return paragraphs[:3]

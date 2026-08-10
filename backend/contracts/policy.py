"""Policy JSON contracts shared by agents, services, and tests."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field, field_validator

from contracts.base import AwmContractModel, AwmFlexibleContractModel


REQUIRED_STEP1_POLICY_FIELDS: List[str] = [
    "policy_title",
    "executive_summary",
    "sections",
    "portfolio",
    "execution",
    "risk_framework",
    "evaluation_metrics",
    "fee_and_governance_notes",
    "disclaimer",
]

STEP1_FORBIDDEN_UI_FIELDS: List[str] = ["menu", "detail"]

REQUIRED_STEP1_SECTION_TITLES: List[str] = [
    "Client Background",
    "Client Financial Snapshot",
    "Client Financial Needs",
    "Client Investment Preferences and Behavioral Considerations",
    "Taxes, Exclusions, and Exemptions",
    "Other Special Requirements",
    "Capital Deployment Timeline",
    "Portfolio Policy",
    "Investment Vehicle Selection Highlights",
    "Risk Management Framework",
    "Policy Evaluation Metrics",
    "Fee and Governance Notes",
    "Disclaimer and Acknowledgment",
]


class Step1PolicySectionContract(AwmFlexibleContractModel):
    """One section in the planner-facing Step-1 policy output."""

    title: str
    content: str

    @field_validator("title", "content")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned


class Step1PortfolioContract(AwmFlexibleContractModel):
    """Step-1 portfolio shape used before mobile UI normalization."""

    recommended_securities: List[Dict[str, Any]]


class Step1ExecutionContract(AwmFlexibleContractModel):
    """Step-1 execution details required by activation planning."""

    remedy_name: str
    funding_source: str
    capital_deployment_timeline: str

    @field_validator("remedy_name", "funding_source", "capital_deployment_timeline")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned


class Step1PolicyContract(AwmFlexibleContractModel):
    """Planner-facing policy contract.

    Extra fields are allowed because older prompts may carry auxiliary analysis
    fields. UI-specific fields remain forbidden by ``validate_step1_policy_schema``.
    """

    policy_title: Any
    executive_summary: Any
    sections: List[Step1PolicySectionContract]
    portfolio: Step1PortfolioContract
    execution: Step1ExecutionContract
    risk_framework: Any
    evaluation_metrics: Any
    fee_and_governance_notes: Any
    disclaimer: Any


class PolicyMenuContract(AwmContractModel):
    title: str
    summary: str


class PolicySectionContract(AwmContractModel):
    id: str
    title: str
    content: str


class PolicySecurityContract(AwmContractModel):
    id: str
    name: str
    allocation_pct: float = Field(ge=0.0)
    allocation_amount: float = Field(ge=0.0)
    management_style: Literal["active", "passive"]
    asset_class: Optional[str] = None


class PolicyDetailPortfolioContract(AwmContractModel):
    currency: str
    total_value: Optional[float] = None
    securities: List[PolicySecurityContract]


class PolicyDetailContract(AwmContractModel):
    title: str
    sections: List[PolicySectionContract]
    portfolio: PolicyDetailPortfolioContract


class PolicyExecutionContract(AwmContractModel):
    remedy_name: str
    funding_source: str
    total_transfer: float = Field(ge=0.0)
    currency: str


class FinalPolicyContract(AwmContractModel):
    """Mobile-facing final policy JSON returned by the investment journey."""

    proposal_count: int = Field(ge=1)
    proposal_index: int = Field(ge=1)
    menu: PolicyMenuContract
    detail: PolicyDetailContract
    execution: PolicyExecutionContract


def validate_step1_policy_schema(payload: Dict[str, Any]) -> None:
    """Validate and lightly repair required Step-1 policy schema fields."""
    missing = [field for field in REQUIRED_STEP1_POLICY_FIELDS if field not in payload]
    if missing:
        raise ValueError(
            f"Step-1 policy JSON missing required fields: {', '.join(missing)}"
        )
    forbidden_fields = [field for field in STEP1_FORBIDDEN_UI_FIELDS if field in payload]
    if forbidden_fields:
        raise ValueError(
            f"Step-1 policy JSON must not contain UI fields: {', '.join(forbidden_fields)}"
        )

    sections = payload.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("Step-1 policy JSON requires a non-empty sections array")
    for idx, section in enumerate(sections):
        if not isinstance(section, dict):
            raise ValueError(f"sections[{idx}] must be an object")
        try:
            Step1PolicySectionContract.model_validate(section)
        except ValueError as exc:
            raise ValueError(f"sections[{idx}] is invalid: {exc}") from exc

    section_titles = [str(section.get("title", "") or "").strip() for section in sections]
    if section_titles != REQUIRED_STEP1_SECTION_TITLES:
        payload["sections"] = _reorder_step1_sections(sections)

    try:
        Step1PolicyContract.model_validate(payload)
    except ValueError as exc:
        raise ValueError(f"Step-1 policy JSON failed contract validation: {exc}") from exc


def normalize_final_policy_json(
    payload: Dict[str, Any],
    securities: List[Dict[str, Any]],
    portfolio: Dict[str, Any],
) -> Dict[str, Any]:
    """Normalize and enforce the single-source final policy JSON contract."""
    menu_raw = payload.get("menu")
    menu = menu_raw if isinstance(menu_raw, dict) else {}
    menu_title = str(menu.get("title", "") or "").strip() or "Recommended Policy"
    menu_summary = str(menu.get("summary", "") or "").strip() or "Policy generated from consultation context and asset allocation optimization."

    detail_raw = payload.get("detail")
    detail = detail_raw if isinstance(detail_raw, dict) else {}
    sections_raw = detail.get("sections")
    sections: List[Dict[str, str]] = []
    if isinstance(sections_raw, list):
        for idx, section in enumerate(sections_raw):
            if not isinstance(section, dict):
                continue
            sec_title = str(section.get("title", "") or "").strip()
            sec_content = str(section.get("content", "") or "").strip()
            if not sec_title or not sec_content:
                continue
            sections.append(
                {
                    "id": str(section.get("id", "") or f"s{idx + 1}"),
                    "title": sec_title,
                    "content": sec_content,
                }
            )
    normalized_titles = [s["title"].strip().lower() for s in sections]
    missing_titles = [
        title
        for title in REQUIRED_STEP1_SECTION_TITLES
        if title.strip().lower() not in normalized_titles
    ]
    if missing_titles:
        raise ValueError(f"Final policy JSON missing required sections: {', '.join(missing_titles)}")

    execution_raw = payload.get("execution")
    execution = execution_raw if isinstance(execution_raw, dict) else {}
    currency = str(portfolio.get("currency", "USD") or "USD").upper()
    total_transfer_model_raw = execution.get("total_transfer")
    total_transfer_portfolio_raw = portfolio.get("total_value")

    deployment_text = ""
    section9_index: Optional[int] = None
    for i, section in enumerate(sections):
        section_title = section["title"].strip().lower()
        if section_title == "capital deployment timeline":
            deployment_text = section["content"]
        if section_title == "investment vehicle selection highlights":
            section9_index = i

    total_transfer = 0.0
    if isinstance(total_transfer_portfolio_raw, (int, float)) and float(total_transfer_portfolio_raw) > 0:
        total_transfer = float(total_transfer_portfolio_raw)
    elif isinstance(total_transfer_model_raw, (int, float)) and float(total_transfer_model_raw) > 0:
        total_transfer = float(total_transfer_model_raw)
    else:
        securities_sum = _sum_security_amounts(securities)
        if securities_sum > 0:
            total_transfer = securities_sum
        else:
            total_transfer = _extract_amount_from_text(deployment_text)
    total_transfer = round(max(0.0, float(total_transfer)), 2)

    normalized_securities: List[Dict[str, Any]] = []
    for idx, sec in enumerate(securities):
        if not isinstance(sec, dict):
            continue
        name = str(sec.get("name", "") or "").strip()
        if not name:
            continue
        allocation_pct = float(sec.get("allocation_pct", 0.0) or 0.0)
        allocation_pct = max(0.0, allocation_pct)
        raw_amount = sec.get("allocation_amount")
        amount = float(raw_amount) if isinstance(raw_amount, (int, float)) else 0.0
        if total_transfer > 0 and allocation_pct > 0:
            amount = (allocation_pct / 100.0) * total_transfer
        management_style_raw = str(sec.get("management_style", "") or "").strip().lower()
        management_style = "active" if management_style_raw == "active" else "passive"
        normalized_securities.append(
            {
                "id": str(sec.get("id", "") or f"sec_{idx + 1}"),
                "name": name,
                "allocation_pct": round(allocation_pct, 2),
                "allocation_amount": round(max(0.0, amount), 2),
                "management_style": management_style,
                "asset_class": str(sec.get("asset_class", "") or "").strip() or None,
            }
        )

    if total_transfer > 0 and normalized_securities:
        amount_sum = round(
            sum(float(row.get("allocation_amount", 0.0) or 0.0) for row in normalized_securities),
            2,
        )
        delta = round(total_transfer - amount_sum, 2)
        if abs(delta) >= 0.01:
            anchor_idx = max(
                range(len(normalized_securities)),
                key=lambda i: float(normalized_securities[i].get("allocation_pct", 0.0) or 0.0),
            )
            adjusted = round(
                max(
                    0.0,
                    float(normalized_securities[anchor_idx].get("allocation_amount", 0.0) or 0.0) + delta,
                ),
                2,
            )
            normalized_securities[anchor_idx]["allocation_amount"] = adjusted

    section9_rows = [
        {
            "security_name": row["name"],
            "asset_class": row["asset_class"] or "",
            "allocation_pct": row["allocation_pct"],
            "allocation_amount": row["allocation_amount"],
            "management_style": row["management_style"],
            "security_id": row["id"],
        }
        for row in sorted(normalized_securities, key=lambda s: float(s["allocation_pct"]), reverse=True)
    ]
    section9_content = json.dumps({"recommended_securities": section9_rows}, ensure_ascii=True)
    if section9_index is not None:
        sections[section9_index]["content"] = section9_content

    final_policy = FinalPolicyContract.model_validate(
        {
            "proposal_count": 1,
            "proposal_index": 1,
            "menu": {
                "title": menu_title,
                "summary": menu_summary,
            },
            "detail": {
                "title": str(detail.get("title", "") or "").strip() or menu_title,
                "sections": sections,
                "portfolio": {
                    "currency": currency,
                    "total_value": total_transfer if total_transfer > 0 else None,
                    "securities": normalized_securities,
                },
            },
            "execution": {
                "remedy_name": str(execution.get("remedy_name", "") or "").strip() or menu_title,
                "funding_source": "JPMorgan Chase Bank, N.A. \u2014 Account ending in XXX",
                "total_transfer": total_transfer,
                "currency": currency,
            },
        }
    )
    return final_policy.model_dump()


def _reorder_step1_sections(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fuzzy-match LLM section titles to the required Step-1 order."""
    required_norm = {_normalize_title(t): t for t in REQUIRED_STEP1_SECTION_TITLES}
    section_by_norm = {_normalize_title(s.get("title", "")): s for s in sections}

    reordered = []
    used = set()
    for req_norm, req_title in required_norm.items():
        if req_norm in section_by_norm and req_norm not in used:
            reordered.append(section_by_norm[req_norm])
            used.add(req_norm)
            continue

        matched = False
        for sn, sec in section_by_norm.items():
            if sn not in used and (req_norm in sn or sn in req_norm):
                reordered.append(sec)
                used.add(sn)
                matched = True
                break
        if not matched:
            reordered.append({"title": req_title, "content": "Not available."})

    return reordered


def _normalize_title(title: str) -> str:
    return title.strip().lower().replace("-", " ").replace("_", " ")


def _extract_amount_from_text(text: str) -> float:
    raw = str(text or "")
    if not raw:
        return 0.0

    direct_matches = re.findall(
        r"(?i)(?:usd|us\\$|\\$)\\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\\.[0-9]+)?|[0-9]+(?:\\.[0-9]+)?)|([0-9]{1,3}(?:,[0-9]{3})*(?:\\.[0-9]+)?|[0-9]+(?:\\.[0-9]+)?)\\s*(?:usd|us\\$)",
        raw,
    )
    if direct_matches:
        value_raw = direct_matches[0][0] or direct_matches[0][1]
        try:
            return max(0.0, float(value_raw.replace(",", "")))
        except ValueError:
            pass

    million_match = re.search(r"(?i)\\b([0-9]+(?:\\.[0-9]+)?)\\s*(million|m)\\b", raw)
    if million_match:
        try:
            return max(0.0, float(million_match.group(1)) * 1_000_000.0)
        except ValueError:
            pass
    return 0.0


def _sum_security_amounts(rows: List[Dict[str, Any]]) -> float:
    total = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        amount = row.get("allocation_amount")
        if isinstance(amount, (int, float)):
            total += max(0.0, float(amount))
    return round(total, 2)

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, List, Mapping
from urllib.parse import urlparse

from advisor.assumptions.providers.contracts import ProviderAdapterError
from advisor.assumptions.providers.registry import build_default_provider_registry
from advisor.agents.quant_contracts.models import (
    QuantEvidenceClaim,
    QuantEvidenceEnvelope,
)


def _public_fact_evidence(
    result: Mapping[str, Any],
) -> QuantEvidenceEnvelope:
    """Validate only the researched fact value as reporting-only evidence."""

    full_result = (
        result.get("full_result")
        if isinstance(result.get("full_result"), dict)
        else {}
    )
    errors: List[str] = []
    if full_result.get("schema_version") != (
        "awm.session_public_fact_authorization.v1"
    ):
        errors.append("public_fact_schema_invalid")
    if set(full_result) != {
        "schema_version",
        "session_fact_id",
        "authorization",
        "fact",
        "sources",
        "durable_promotion",
        "disclosure",
    }:
        errors.append("public_fact_fields_invalid")

    session_fact_id = str(full_result.get("session_fact_id") or "").strip()
    if not re.fullmatch(r"session-public-fact:[a-f0-9]{32}", session_fact_id):
        errors.append("public_fact_id_invalid")

    authorization = (
        full_result.get("authorization")
        if isinstance(full_result.get("authorization"), dict)
        else {}
    )
    required_authorization = {
        "scope": "current_financial_planning_session",
        "human_review_required": False,
        "durable": False,
        "reporting_allowed": True,
        "durable_model_input_allowed": False,
        "recommendation_allowed": False,
    }
    if any(
        authorization.get(key) != expected
        for key, expected in required_authorization.items()
    ):
        errors.append("public_fact_authorization_invalid")
    if set(authorization) != {
        "scope",
        "session_scope_sha256",
        "expires_at",
        "human_review_required",
        "durable",
        "reporting_allowed",
        "session_calculation_allowed",
        "durable_model_input_allowed",
        "recommendation_allowed",
    } or not re.fullmatch(
        r"sha256:[a-f0-9]{64}",
        str(authorization.get("session_scope_sha256") or ""),
    ):
        errors.append("public_fact_authorization_fields_invalid")
    expires_at = _aware_timestamp(authorization.get("expires_at"))
    if expires_at is None or expires_at <= datetime.now(timezone.utc):
        errors.append("public_fact_authorization_expired")

    fact = (
        full_result.get("fact")
        if isinstance(full_result.get("fact"), dict)
        else {}
    )
    if set(fact) != {
        "variable_key",
        "effective_year",
        "value",
        "unit",
        "jurisdiction",
        "content_sha256",
        "retrieved_at",
        "origin",
    }:
        errors.append("public_fact_fields_invalid")
    variable_key = str(fact.get("variable_key") or "").strip()
    effective_year = fact.get("effective_year")
    expected_unit = _EXPECTED_RESEARCH_UNITS.get(variable_key)
    if expected_unit is None:
        errors.append("public_fact_variable_unsupported")
    if (
        isinstance(effective_year, bool)
        or not isinstance(effective_year, int)
        or not 2000 <= effective_year <= 2200
    ):
        errors.append("public_fact_effective_year_invalid")
    if expected_unit is not None and fact.get("unit") != expected_unit:
        errors.append("public_fact_unit_invalid")
    if not re.fullmatch(
        r"sha256:[a-f0-9]{64}",
        str(fact.get("content_sha256") or ""),
    ):
        errors.append("public_fact_content_hash_invalid")
    if _aware_timestamp(fact.get("retrieved_at")) is None:
        errors.append("public_fact_retrieved_at_invalid")
    if fact.get("origin") not in {"live_research", "durable_registry"}:
        errors.append("public_fact_origin_invalid")
    provider = build_default_provider_registry().provider_for_variable(
        variable_key
    )
    value_shape_valid = False
    if provider is None:
        errors.append("public_fact_value_invalid")
    else:
        try:
            provider.validate_value(variable_key, fact.get("value"))
        except ProviderAdapterError:
            errors.append("public_fact_value_invalid")
        else:
            value_shape_valid = True

    sources = full_result.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 3:
        errors.append("public_fact_sources_invalid")
        sources = []
    source_urls: List[str] = []
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            errors.append(f"public_fact_source_{index}_invalid")
            continue
        if set(source) != {"publisher", "title", "url", "published_at"}:
            errors.append(f"public_fact_source_{index}_fields_invalid")
            continue
        publisher = str(source.get("publisher") or "").strip()
        title = str(source.get("title") or "").strip()
        url = str(source.get("url") or "").strip()
        parsed = urlparse(url)
        if (
            not publisher
            or not title
            or parsed.scheme != "https"
            or not parsed.hostname
        ):
            errors.append(f"public_fact_source_{index}_invalid")
            continue
        source_urls.append(url)

    claims: List[QuantEvidenceClaim] = []
    if "value" not in fact:
        errors.append("public_fact_value_missing")
    elif value_shape_valid:
        for path, value in _fact_numeric_leaves(fact.get("value")):
            unit = _claim_unit(variable_key, path)
            if unit is None:
                errors.append(f"public_fact_value_unit_unsupported:{path}")
                continue
            leaf_key = _claim_key(path)
            metric_key = (
                variable_key
                if path == "$"
                else f"{variable_key}.{leaf_key}"
            )
            claim_id = re.sub(
                r"[^A-Za-z0-9_-]+", "_", metric_key
            ).strip("_")[:160]
            claims.append(
                QuantEvidenceClaim(
                    metric_key=metric_key,
                    value=value,
                    value_decimal=str(value),
                    unit=unit,
                    source_path=(
                        "$.full_result.fact.value"
                        + ("" if path == "$" else path[1:])
                    ),
                    claim_id=claim_id,
                    evidence_ref=f"research_public_financial_fact/{claim_id}",
                    semantic_metric_keys=[variable_key],
                )
            )
    if not claims:
        errors.append("public_fact_claims_missing")

    promotion = (
        full_result.get("durable_promotion")
        if isinstance(full_result.get("durable_promotion"), dict)
        else {}
    )
    if set(promotion) != {
        "schema_version",
        "status",
        "reason_codes",
        "examination_id",
        "durable_assumption_id",
        "durable_version",
        "supersedes_artifact_id",
        "granted_uses",
        "agent_assessment",
        "verification",
        "policy",
    } or promotion.get("schema_version") != (
        "awm.durable_fact_promotion_receipt.v1"
    ):
        errors.append("public_fact_promotion_receipt_invalid")
    promotion_status = promotion.get("status")
    if promotion.get("agent_assessment") is not None:
        errors.append("public_fact_promotion_review_must_be_separate")
    if promotion_status not in {
        "promoted",
        "already_current",
        "session_only",
    }:
        errors.append("public_fact_promotion_status_invalid")
    granted_uses = promotion.get("granted_uses")
    if not isinstance(granted_uses, list) or any(
        use not in {"reporting", "model_input"} for use in granted_uses
    ):
        errors.append("public_fact_promotion_uses_invalid")
    if promotion_status in {"promoted", "already_current"}:
        if (
            not str(promotion.get("examination_id") or "").startswith(
                "durable-fact-examination:"
            )
            or not str(promotion.get("durable_assumption_id") or "").startswith(
                "approved:"
            )
            or not isinstance(promotion.get("durable_version"), int)
            or set(granted_uses or []).difference({"reporting", "model_input"})
            or "reporting" not in (granted_uses or [])
            or not isinstance(promotion.get("verification"), dict)
            or not isinstance(promotion.get("policy"), dict)
            or promotion.get("reason_codes") != []
        ):
            errors.append("public_fact_promotion_authorization_invalid")
    elif (
        not isinstance(promotion.get("reason_codes"), list)
        or not promotion.get("reason_codes")
        or promotion.get("durable_assumption_id") is not None
        or promotion.get("durable_version") is not None
        or granted_uses != []
    ):
        errors.append("public_fact_session_only_promotion_invalid")

    execution_ok = result.get("ok") is True
    valid = bool(execution_ok and claims and source_urls and not errors)
    disclosure = str(full_result.get("disclosure") or "").strip()
    return QuantEvidenceEnvelope(
        tool="research_public_financial_fact",
        status="complete" if valid else "invalid",
        execution_ok=execution_ok,
        valid_for_reporting=valid,
        valid_for_conclusion=False,
        valid_for_recommendation=False,
        claims=claims,
        warnings=[disclosure] if valid and disclosure else [],
        assumptions=(
            ["Use is limited to the current authenticated session."]
            if valid
            else []
        ),
        errors=errors,
    )


def _public_fact_reuse_receipt_evidence(
    result: Mapping[str, Any],
) -> QuantEvidenceEnvelope:
    """Recognize a reuse receipt without exposing receipt metadata as claims."""

    execution_ok = result.get("ok") is True
    full_result = result.get("full_result")
    valid_receipt = bool(
        execution_ok
        and isinstance(full_result, dict)
        and full_result.get("schema_version")
        == "awm.public_fact_reuse_review.v1"
        and re.fullmatch(
            r"session-public-fact:[a-f0-9]{32}",
            str(full_result.get("session_fact_id") or ""),
        )
        and isinstance(full_result.get("agent_assessment"), dict)
        and isinstance(full_result.get("durable_promotion"), dict)
        and isinstance(full_result.get("immediate_session_use"), dict)
    )
    return QuantEvidenceEnvelope(
        tool="review_public_fact_reuse",
        status="complete" if valid_receipt else "invalid",
        execution_ok=execution_ok,
        valid_for_reporting=False,
        valid_for_conclusion=False,
        valid_for_recommendation=False,
        claims=[],
        errors=[] if valid_receipt else ["public_fact_reuse_receipt_invalid"],
    )


_EXPECTED_RESEARCH_UNITS = {
    "federal_standard_deduction": "USD_by_filing_status",
    "federal_tax_brackets": "USD_thresholds_and_percent_rates",
    "retirement_contribution_limits": "USD_annual_limits",
    "social_security_cola": "percent",
    "social_security_taxable_maximum": "USD_annual",
    "medicare_part_b_premium": "USD_per_month",
}


def _fact_numeric_leaves(value: Any, path: str = "$") -> List[tuple[str, Any]]:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return [(path, value)]
    output: List[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            output.extend(_fact_numeric_leaves(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            output.extend(_fact_numeric_leaves(child, f"{path}[{index}]"))
    return output


def _claim_unit(variable_key: str, path: str) -> str | None:
    if variable_key == "social_security_cola":
        return "percentage" if path == "$" else None
    if variable_key == "social_security_taxable_maximum":
        return "money_per_year:USD" if path == "$" else None
    if variable_key == "medicare_part_b_premium":
        return "money_per_month:USD" if path == "$" else None
    if variable_key == "federal_standard_deduction":
        return "money:USD"
    if variable_key == "retirement_contribution_limits":
        return "money_per_year:USD"
    if variable_key == "federal_tax_brackets":
        return "percentage" if path.endswith(".rate_percent") else "money:USD"
    return None


def _claim_key(path: str) -> str:
    return path.removeprefix("$.").replace("[", ".").replace("]", "")


def _aware_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


__all__ = ["_public_fact_evidence", "_public_fact_reuse_receipt_evidence"]

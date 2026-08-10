from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from advisor.agents.quant_contracts._shared import _numeric_leaves


class QuantEvidenceClaim(BaseModel):
    """One value whose provenance can be checked without model judgment."""

    model_config = ConfigDict(extra="forbid")

    metric_key: str
    value: Any
    value_decimal: Optional[str] = None
    display_value: Optional[str] = None
    unit: str
    source_path: str
    claim_id: str = ""
    evidence_ref: str = ""
    semantic_metric_keys: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _populate_stable_reference(self) -> "QuantEvidenceClaim":
        """Give every reportable value an agent-visible, deterministic reference."""

        if not self.claim_id:
            self.claim_id = self.metric_key
        if not self.evidence_ref:
            self.evidence_ref = f"{self.metric_key}@{self.source_path}"
        self.semantic_metric_keys = list(
            dict.fromkeys(
                str(key).strip()
                for key in self.semantic_metric_keys
                if str(key).strip() and str(key).strip() != self.metric_key
            )
        )
        return self


class QuantEvidenceEnvelope(BaseModel):
    """Typed evidence derived from one deterministic quantitative tool result."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["awm.quant_evidence.v1"] = "awm.quant_evidence.v1"
    tool: str
    status: Literal["complete", "blocked", "invalid"]
    execution_ok: bool = False
    valid_for_reporting: bool
    valid_for_conclusion: bool = False
    valid_for_recommendation: bool
    permitted_use: Literal["none", "reporting", "conclusion", "recommendation"] = "none"
    conclusion_code: Optional[str] = None
    claims: List[QuantEvidenceClaim] = Field(default_factory=list)
    constraint_checks: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_permission_hierarchy(self) -> "QuantEvidenceEnvelope":
        """Keep execution and evidence permissions separate but internally coherent."""

        if self.valid_for_recommendation:
            self.valid_for_conclusion = True
        if self.valid_for_conclusion:
            self.valid_for_reporting = True
        if self.valid_for_reporting:
            self.execution_ok = True
        self.permitted_use = _highest_permitted_use(
            reporting=self.valid_for_reporting,
            conclusion=self.valid_for_conclusion,
            recommendation=self.valid_for_recommendation,
        )
        return self


class QuantValidationIssue(BaseModel):
    """Machine-readable reason a client-facing claim was blocked."""

    model_config = ConfigDict(extra="forbid")

    type: str
    detail: str
    claim: Optional[str] = None


class QuantConclusionValidation(BaseModel):
    """Final deterministic verdict attached to every SDK response."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["awm.quant_conclusion_validation.v1"] = (
        "awm.quant_conclusion_validation.v1"
    )
    status: Literal["not_applicable", "passed", "abstained", "blocked"]
    valid_for_reporting: bool = False
    valid_for_conclusion: bool = False
    valid_for_recommendation: bool
    permitted_use: Literal["none", "reporting", "conclusion", "recommendation"] = "none"
    recommendation_policy_enabled: bool
    quant_tools_seen: List[str] = Field(default_factory=list)
    evidence: List[QuantEvidenceEnvelope] = Field(default_factory=list)
    errors: List[QuantValidationIssue] = Field(default_factory=list)
    sanitized_errors: List[QuantValidationIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_permission(self) -> "QuantConclusionValidation":
        if self.valid_for_recommendation:
            self.valid_for_conclusion = True
        if self.valid_for_conclusion:
            self.valid_for_reporting = True
        self.permitted_use = _highest_permitted_use(
            reporting=self.valid_for_reporting,
            conclusion=self.valid_for_conclusion,
            recommendation=self.valid_for_recommendation,
        )
        return self


class QuantResponseAnnotations(BaseModel):
    """Structured claim references parsed from the final quantitative response."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["awm.quant_response_annotations.v1"] = (
        "awm.quant_response_annotations.v1"
    )
    status: Literal["not_applicable", "complete", "missing", "invalid"]
    cited_claim_ids: List[str] = Field(default_factory=list)
    available_claim_ids: List[str] = Field(default_factory=list)
    invalid_claim_ids: List[str] = Field(default_factory=list)
    citation_syntax: str = "[evidence: tool/claim_id]"


def _highest_permitted_use(
    *,
    reporting: bool,
    conclusion: bool,
    recommendation: bool,
) -> Literal["none", "reporting", "conclusion", "recommendation"]:
    if recommendation:
        return "recommendation"
    if conclusion:
        return "conclusion"
    if reporting:
        return "reporting"
    return "none"


def _cashflow_metric_claim(
    metric_key: str,
    payload: Any,
) -> tuple[Optional[QuantEvidenceClaim], List[str]]:
    """Validate the typed/provenanced metric boundary used for factual reporting."""

    return _typed_metric_claim(metric_key, payload, namespace="cashflow")


def _typed_metric_claim(
    metric_key: str,
    payload: Any,
    *,
    namespace: str,
) -> tuple[Optional[QuantEvidenceClaim], List[str]]:
    prefix = f"{namespace}_metric_invalid:{metric_key}"
    errors: List[str] = []
    if not isinstance(payload, dict):
        return None, [f"{prefix}:not_object"]
    unit = payload.get("unit")
    if not isinstance(unit, str) or not unit.strip():
        errors.append(f"{prefix}:unit_missing")
    value = payload.get("value")
    allowed_boolean_metrics = {
        "material_change",
        "target_volatility_passed",
    }
    canonical_boolean = (
        isinstance(value, bool)
        and unit == "boolean"
        and metric_key in allowed_boolean_metrics
    )
    if "value" not in payload or (not _numeric_leaves(value) and not canonical_boolean):
        errors.append(f"{prefix}:non_numeric_value")
    source_path = payload.get("source_path")
    if not isinstance(source_path, str) or not source_path.strip().startswith("$."):
        errors.append(f"{prefix}:source_path_missing")
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        errors.append(f"{prefix}:provenance_missing")
    else:
        provenance_path = provenance.get("source_path")
        if provenance_path != source_path:
            errors.append(f"{prefix}:provenance_path_mismatch")
        derivation = provenance.get("derivation")
        if not isinstance(derivation, str) or not derivation.strip():
            errors.append(f"{prefix}:derivation_missing")
    if errors:
        return None, errors
    return (
        QuantEvidenceClaim(
            metric_key=metric_key,
            value=payload["value"],
            unit=unit.strip(),
            source_path=source_path.strip(),
        ),
        [],
    )

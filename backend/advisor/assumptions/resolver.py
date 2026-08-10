"""Shadow-only resolution of approved public assumptions.

The resolver reports what an approved assumption would change. It never mutates
the active model input and defaults to disabled.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from advisor.assumptions.contracts import PermittedUse, SourceClass
from advisor.assumptions.governance import GovernedAssumptionRepository
from advisor.assumptions.providers.refresh import HIGHER_PRECEDENCE_SOURCES
from advisor.assumptions.registry import (
    VariableSourceRegistry,
    load_variable_source_registry,
)


class AssumptionIntegrationMode(str, Enum):
    OFF = "off"
    SHADOW = "shadow"


class ShadowSelectionReason(str, Enum):
    DISABLED = "disabled"
    HIGHER_PRECEDENCE_VALUE = "higher_precedence_value"
    APPROVED_AUTHORITATIVE_AVAILABLE = "approved_authoritative_available"
    APPROVED_STALE = "approved_stale"
    APPROVED_NOT_MODEL_ELIGIBLE = "approved_not_model_eligible"
    NO_APPROVED_VALUE = "no_approved_value"
    VARIABLE_NOT_PUBLIC = "variable_not_public"
    VARIABLE_NOT_REGISTERED = "variable_not_registered"


class ShadowComparison(str, Enum):
    NO_CHANGE = "no_change"
    DIFFERENT = "different"
    NO_APPROVED_VALUE = "no_approved_value"
    NOT_COMPARABLE = "not_comparable"


class ShadowAssumptionResolution(BaseModel):
    """Comparison only; ``active_value`` is always preserved."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    variable_key: str
    effective_year: int
    active_value: Any
    active_source_id: str
    normalized_active_source: str
    active_value_preserved: bool = True
    shadow_only: bool = True
    selection_reason: ShadowSelectionReason
    comparison: ShadowComparison
    would_select_value: Any
    would_select_source_id: str
    approved_artifact_id: str | None = None
    approved_version: int | None = Field(default=None, ge=1)
    absolute_delta: float | None = None
    relative_delta_percent: float | None = None


def assumption_integration_mode() -> AssumptionIntegrationMode:
    raw = os.getenv("AWM_ASSUMPTION_INTEGRATION_MODE", "off").strip().lower()
    try:
        return AssumptionIntegrationMode(raw)
    except ValueError as exc:
        raise RuntimeError(
            "AWM_ASSUMPTION_INTEGRATION_MODE must be one of: off, shadow"
        ) from exc


class ShadowAssumptionResolver:
    def __init__(
        self,
        *,
        repository: GovernedAssumptionRepository,
        source_policy: VariableSourceRegistry | None = None,
    ) -> None:
        self.repository = repository
        self.source_policy = source_policy or load_variable_source_registry()

    def resolve(
        self,
        *,
        variable_key: str,
        effective_year: int,
        active_value: Any,
        active_source_id: str,
        mode: AssumptionIntegrationMode | str | None = None,
        as_of: datetime | None = None,
    ) -> ShadowAssumptionResolution:
        mode = AssumptionIntegrationMode(mode or assumption_integration_mode())
        as_of = as_of or datetime.now(timezone.utc)
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        normalized_source = self.source_policy.normalize_source(active_source_id)
        common = {
            "variable_key": variable_key,
            "effective_year": effective_year,
            "active_value": active_value,
            "active_source_id": active_source_id,
            "normalized_active_source": normalized_source,
            "would_select_value": active_value,
            "would_select_source_id": active_source_id,
        }
        if mode is AssumptionIntegrationMode.OFF:
            return ShadowAssumptionResolution(
                selection_reason=ShadowSelectionReason.DISABLED,
                comparison=ShadowComparison.NOT_COMPARABLE,
                **common,
            )

        policy = self.source_policy.get(variable_key)
        if policy is None:
            return ShadowAssumptionResolution(
                selection_reason=ShadowSelectionReason.VARIABLE_NOT_REGISTERED,
                comparison=ShadowComparison.NOT_COMPARABLE,
                **common,
            )
        if policy.source_class is not SourceClass.PUBLIC_AUTHORITATIVE:
            return ShadowAssumptionResolution(
                selection_reason=ShadowSelectionReason.VARIABLE_NOT_PUBLIC,
                comparison=ShadowComparison.NOT_COMPARABLE,
                **common,
            )

        approved = self.repository.latest_approved(
            variable_key,
            effective_year=effective_year,
        )
        if normalized_source in HIGHER_PRECEDENCE_SOURCES:
            return ShadowAssumptionResolution(
                selection_reason=ShadowSelectionReason.HIGHER_PRECEDENCE_VALUE,
                comparison=ShadowComparison.NO_CHANGE,
                approved_artifact_id=approved.artifact_id if approved else None,
                approved_version=(
                    approved.assumption_set_version if approved else None
                ),
                **common,
            )
        if approved is None:
            return ShadowAssumptionResolution(
                selection_reason=ShadowSelectionReason.NO_APPROVED_VALUE,
                comparison=ShadowComparison.NO_APPROVED_VALUE,
                **common,
            )
        if _approved_artifact_is_stale(
            approved,
            maximum_age_days=policy.maximum_age_days,
            as_of=as_of,
        ):
            return ShadowAssumptionResolution(
                selection_reason=ShadowSelectionReason.APPROVED_STALE,
                comparison=ShadowComparison.NOT_COMPARABLE,
                approved_artifact_id=approved.artifact_id,
                approved_version=approved.assumption_set_version,
                **common,
            )
        if PermittedUse.MODEL_INPUT not in approved.permitted_uses:
            return ShadowAssumptionResolution(
                selection_reason=(
                    ShadowSelectionReason.APPROVED_NOT_MODEL_ELIGIBLE
                ),
                comparison=ShadowComparison.NOT_COMPARABLE,
                approved_artifact_id=approved.artifact_id,
                approved_version=approved.assumption_set_version,
                **common,
            )

        comparison, absolute_delta, relative_delta = _compare(
            active_value,
            approved.value,
        )
        return ShadowAssumptionResolution(
            selection_reason=(
                ShadowSelectionReason.APPROVED_AUTHORITATIVE_AVAILABLE
            ),
            comparison=comparison,
            would_select_value=approved.value,
            would_select_source_id="approved_assumption_registry",
            approved_artifact_id=approved.artifact_id,
            approved_version=approved.assumption_set_version,
            absolute_delta=absolute_delta,
            relative_delta_percent=relative_delta,
            **{
                key: value
                for key, value in common.items()
                if key not in {"would_select_value", "would_select_source_id"}
            },
        )


def build_shadow_assumption_report(
    *,
    resolver: ShadowAssumptionResolver,
    effective_year: int,
    active_values: dict[str, Any],
    source_by_variable: dict[str, str],
    required_variables: tuple[str, ...],
    mode: AssumptionIntegrationMode | str | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate only variables the caller says this projection requires."""

    selected_mode = AssumptionIntegrationMode(
        mode or assumption_integration_mode()
    )
    resolutions = []
    for variable_key in required_variables:
        if variable_key not in active_values:
            continue
        resolutions.append(
            resolver.resolve(
                variable_key=variable_key,
                effective_year=effective_year,
                active_value=active_values[variable_key],
                active_source_id=source_by_variable.get(
                    variable_key,
                    "configured_yaml_default",
                ),
                mode=selected_mode,
                as_of=as_of,
            )
        )
    return {
        "schema_version": "awm.assumption_shadow_report.v1",
        "mode": selected_mode.value,
        "effective_year": effective_year,
        "required_variables": list(required_variables),
        "evaluated_variables": [
            resolution.variable_key for resolution in resolutions
        ],
        "model_inputs_changed": False,
        "resolutions": [
            resolution.model_dump(mode="json") for resolution in resolutions
        ],
    }


def attach_shadow_assumption_report(
    engine_payload: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    """Attach audit metadata to a copied payload without changing model values."""

    copied = copy.deepcopy(engine_payload)
    contract = copied.setdefault("awm_input_contract", {})
    contract["authoritative_assumption_shadow"] = copy.deepcopy(report)
    return copied


def _compare(
    active_value: Any,
    approved_value: Any,
) -> tuple[ShadowComparison, float | None, float | None]:
    if (
        isinstance(active_value, (int, float))
        and not isinstance(active_value, bool)
        and isinstance(approved_value, (int, float))
        and not isinstance(approved_value, bool)
    ):
        delta = float(approved_value) - float(active_value)
        relative = (
            (delta / abs(float(active_value))) * 100
            if float(active_value) != 0
            else None
        )
        return (
            ShadowComparison.NO_CHANGE
            if delta == 0
            else ShadowComparison.DIFFERENT,
            delta,
            relative,
        )
    active_json = json.dumps(
        active_value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    approved_json = json.dumps(
        approved_value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return (
        ShadowComparison.NO_CHANGE
        if active_json == approved_json
        else ShadowComparison.DIFFERENT,
        None,
        None,
    )


def _approved_artifact_is_stale(
    artifact: Any,
    *,
    maximum_age_days: int | None,
    as_of: datetime,
) -> bool:
    if artifact.effective_to is not None and as_of > artifact.effective_to:
        return True
    if maximum_age_days is None:
        return False
    timestamps = [
        evidence.retrieved_at for evidence in artifact.evidence
    ] or [artifact.created_at]
    latest = max(timestamps)
    if latest.tzinfo is None:
        raise ValueError("assumption retrieval time must be timezone-aware")
    return (as_of - latest).days > maximum_age_days

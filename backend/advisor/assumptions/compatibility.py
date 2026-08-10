"""Adapters that extend existing source contracts without replacing them."""

from __future__ import annotations

from typing import Any

from advisor.assumptions.contracts import PermittedUse
from advisor.assumptions.registry import (
    VariableSourceRegistry,
    load_variable_source_registry,
)


def build_variable_source_policy_context(
    source_by_field: dict[str, Any],
    *,
    requested_use: PermittedUse | str = PermittedUse.MODEL_INPUT,
    registry: VariableSourceRegistry | None = None,
) -> dict[str, Any]:
    """Describe current source compatibility in observe-only form.

    This adapter is deliberately additive. It neither rewrites the legacy
    ``source_by_field`` mapping nor changes readiness or recommendation status.
    """

    registry = registry or load_variable_source_registry()
    decisions = [
        registry.validate_source_use(
            variable_key=str(variable_key),
            source_id=str(source_id),
            requested_use=requested_use,
        )
        for variable_key, source_id in sorted(
            (source_by_field or {}).items(),
            key=lambda item: str(item[0]),
        )
        if str(variable_key).strip() and str(source_id).strip()
    ]
    serialized = [decision.model_dump(mode="json") for decision in decisions]
    counts = {
        status: sum(decision.status == status for decision in decisions)
        for status in ("compatible", "incompatible", "unclassified")
    }
    return {
        "schema_version": "awm.variable_source_policy_context.v1",
        "policy_id": registry.document.policy_id,
        "policy_version": registry.document.policy_version,
        "enforcement_mode": registry.document.enforcement_mode,
        "requested_use": PermittedUse(requested_use).value,
        "legacy_source_contract_preserved": True,
        "decisions": serialized,
        "summary": {
            **counts,
            "checked": len(decisions),
        },
        "violations": [
            {
                "variable_key": decision.variable_key,
                "source_id": decision.source_id,
                "violations": list(decision.violations),
            }
            for decision in decisions
            if decision.violations
        ],
        "recommendation_blockers": (
            []
            if registry.document.enforcement_mode == "observe_only"
            else [
                f"{decision.variable_key}:{violation}"
                for decision in decisions
                for violation in decision.violations
            ]
        ),
    }

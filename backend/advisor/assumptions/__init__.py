"""Governed, compatibility-first source policies for planning variables."""

from advisor.assumptions.compatibility import build_variable_source_policy_context
from advisor.assumptions.contracts import (
    AssumptionArtifact,
    AssumptionEvidence,
    AssumptionStatus,
    MissingBehavior,
    PermittedUse,
    SourceClass,
    SourceUseDecision,
    VariableSourcePolicy,
)
from advisor.assumptions.registry import (
    VariableSourceRegistry,
    load_variable_source_registry,
)

__all__ = [
    "AssumptionArtifact",
    "AssumptionEvidence",
    "AssumptionStatus",
    "MissingBehavior",
    "PermittedUse",
    "SourceClass",
    "SourceUseDecision",
    "VariableSourcePolicy",
    "VariableSourceRegistry",
    "build_variable_source_policy_context",
    "load_variable_source_registry",
]

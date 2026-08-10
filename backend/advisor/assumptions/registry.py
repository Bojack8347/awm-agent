"""Loader and deterministic evaluator for the variable-source policy."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from advisor.assumptions.contracts import (
    PermittedUse,
    SourceUseDecision,
    VariableSourcePolicy,
    VariableSourcePolicyDocument,
)


DEFAULT_POLICY_PATH = Path(__file__).with_name("variable_source_policy.v1.json")


class VariableSourceRegistry:
    """Expanded, immutable view of the compact repository policy."""

    def __init__(self, document: VariableSourcePolicyDocument):
        self.document = document
        policies: dict[str, VariableSourcePolicy] = {}
        for group in document.groups:
            for policy in group.expand():
                if policy.variable_key in policies:
                    raise ValueError(
                        f"duplicate variable-source policy: {policy.variable_key}"
                    )
                policies[policy.variable_key] = policy
        self._policies = policies

    @classmethod
    def from_path(cls, path: Path) -> "VariableSourceRegistry":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(VariableSourcePolicyDocument.model_validate(raw))

    def get(self, variable_key: str) -> VariableSourcePolicy | None:
        return self._policies.get(str(variable_key or "").strip())

    def require(self, variable_key: str) -> VariableSourcePolicy:
        policy = self.get(variable_key)
        if policy is None:
            raise KeyError(f"variable-source policy not found: {variable_key}")
        return policy

    def variable_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._policies))

    def normalize_source(self, source_id: str) -> str:
        source = str(source_id or "").strip()
        return self.document.source_aliases.get(source, source)

    def validate_source_use(
        self,
        *,
        variable_key: str,
        source_id: str,
        requested_use: PermittedUse | str = PermittedUse.MODEL_INPUT,
    ) -> SourceUseDecision:
        requested_use = PermittedUse(requested_use)
        source = str(source_id or "").strip()
        normalized_source = self.normalize_source(source)
        policy = self.get(variable_key)
        if policy is None:
            return SourceUseDecision(
                policy_id=self.document.policy_id,
                policy_version=self.document.policy_version,
                enforcement_mode=self.document.enforcement_mode,
                variable_key=variable_key,
                source_id=source,
                normalized_source=normalized_source,
                source_class=None,
                requested_use=requested_use,
                status="unclassified",
                allowed=False,
                violations=("variable_not_registered",),
            )

        violations: list[str] = []
        warnings: list[str] = []
        if normalized_source not in policy.allowed_sources:
            violations.append("source_not_allowed_for_variable")
        if requested_use not in policy.allowed_uses:
            violations.append("use_not_allowed_for_variable")
        if normalized_source == "unconfirmed_draft_fact":
            warnings.append("unconfirmed_draft_source")
        if normalized_source == "client_authorized_configured_default":
            warnings.append("configured_default_must_remain_disclosed")

        return SourceUseDecision(
            policy_id=self.document.policy_id,
            policy_version=self.document.policy_version,
            enforcement_mode=self.document.enforcement_mode,
            variable_key=variable_key,
            source_id=source,
            normalized_source=normalized_source,
            source_class=policy.source_class,
            requested_use=requested_use,
            status="compatible" if not violations else "incompatible",
            allowed=not violations,
            violations=tuple(violations),
            warnings=tuple(warnings),
        )


@lru_cache(maxsize=1)
def load_variable_source_registry() -> VariableSourceRegistry:
    """Load the repository policy once per process."""

    return VariableSourceRegistry.from_path(DEFAULT_POLICY_PATH)

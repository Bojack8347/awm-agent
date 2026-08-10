"""Constrained public-variable research for ephemeral or durable use.

The Research Specialist never receives Client File data and never activates a
durable value. It can return a server-validated, session-bound public fact for
immediate reporting/local arithmetic, or create a reporting-only candidate for
the existing governance workflow. Durable promotion is a separate server-owned
boundary and is never selected by this specialist.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import RLock
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from advisor.assumptions.contracts import (
    AssumptionArtifact,
    AssumptionEvidence,
    AssumptionStatus,
    PermittedUse,
    SourceClass,
)
from advisor.assumptions.governance import GovernedAssumptionRepository
from advisor.assumptions.providers.contracts import (
    ProviderAdapterError,
    ProviderCandidateBatch,
    ProviderRequest,
)
from advisor.assumptions.providers.registry import AuthoritativeProviderRegistry
from advisor.assumptions.registry import (
    VariableSourceRegistry,
    load_variable_source_registry,
)


SESSION_PUBLIC_FACT_TTL = timedelta(minutes=15)


class ResearchErrorCode(str, Enum):
    POLICY_DENIED = "research_policy_denied"
    RULE_NOT_CONFIGURED = "research_rule_not_configured"
    RECENT_ATTEMPT = "research_recently_attempted"
    GATEWAY_UNAVAILABLE = "research_gateway_unavailable"
    GATEWAY_AUTHENTICATION_FAILED = (
        "research_gateway_authentication_failed"
    )
    GATEWAY_QUOTA_EXHAUSTED = "research_gateway_quota_exhausted"
    GATEWAY_RATE_LIMITED = "research_gateway_rate_limited"
    NO_RESULT = "research_no_result"
    OUTPUT_INVALID = "research_output_invalid"
    SOURCE_NOT_ALLOWED = "research_source_not_allowed"
    VALUE_INVALID = "research_value_invalid"
    PERSISTENCE_FAILED = "research_persistence_failed"


class ResearchSpecialistError(RuntimeError):
    """Stable terminal error returned by the bounded research path."""

    def __init__(
        self,
        code: ResearchErrorCode,
        message: str,
        *,
        variable_key: str,
        effective_year: int,
        attempted: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.variable_key = variable_key
        self.effective_year = effective_year
        self.attempted = attempted


class ResearchRequest(BaseModel):
    """Public-only input supplied to a research gateway."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    variable_key: str = Field(min_length=1, max_length=160)
    effective_year: int = Field(ge=2000, le=2200)


class ResearchRule(BaseModel):
    """Server-owned allowlist and shape expectations for one variable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    variable_key: str = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=240)
    expected_unit: str = Field(min_length=1, max_length=80)
    jurisdiction: str = Field(min_length=1, max_length=160)
    allowed_domains: tuple[str, ...] = Field(min_length=1, max_length=20)
    allowed_publishers: tuple[str, ...] = Field(min_length=1, max_length=20)


class ResearchSource(BaseModel):
    """One primary source actually consulted by the research gateway."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    publisher: str = Field(min_length=1, max_length=240)
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=8, max_length=2000)
    published_at: datetime | None

    @model_validator(mode="after")
    def validate_publication_time(self) -> "ResearchSource":
        if self.published_at is not None and self.published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        return self


class ResearchFinding(BaseModel):
    """Typed candidate content returned by the read-only gateway."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    variable_key: str = Field(min_length=1, max_length=160)
    effective_year: int = Field(ge=2000, le=2200)
    value: Any
    unit: str = Field(min_length=1, max_length=80)
    jurisdiction: str = Field(min_length=1, max_length=160)
    sources: tuple[ResearchSource, ...] = Field(min_length=1, max_length=3)


class SessionPublicFact(BaseModel):
    """Ephemeral public fact bound to one authenticated server session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["awm.session_public_fact.v1"] = (
        "awm.session_public_fact.v1"
    )
    fact_id: str = Field(pattern=r"^session-public-fact:[a-f0-9]{32}$")
    content_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    session_scope_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    variable_key: str = Field(min_length=1, max_length=160)
    effective_year: int = Field(ge=2000, le=2200)
    value: Any
    unit: str = Field(min_length=1, max_length=80)
    jurisdiction: str = Field(min_length=1, max_length=160)
    sources: tuple[ResearchSource, ...] = Field(min_length=1, max_length=3)
    retrieved_at: datetime
    expires_at: datetime
    origin: Literal["live_research", "durable_registry"] = "live_research"
    reporting_allowed: Literal[True] = True
    session_calculation_allowed: bool
    durable: Literal[False] = False
    human_review_required: Literal[False] = False
    durable_model_input_allowed: Literal[False] = False
    recommendation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_session_window(self) -> "SessionPublicFact":
        if self.retrieved_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("session public fact timestamps must be timezone-aware")
        if self.expires_at <= self.retrieved_at:
            raise ValueError("session public fact must expire after retrieval")
        if self.expires_at - self.retrieved_at > SESSION_PUBLIC_FACT_TTL:
            raise ValueError("session public fact expiry exceeds the server limit")
        return self


def research_finding_content_sha256(finding: ResearchFinding) -> str:
    """Return the canonical normalized-finding digest (not a page-content hash)."""

    canonical = json.dumps(
        finding.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def session_public_fact_identity(
    finding: ResearchFinding,
    *,
    session_scope_sha256: str,
) -> tuple[str, str]:
    """Bind a canonical finding to one authenticated session scope."""

    digest = research_finding_content_sha256(finding)
    scoped_digest = hashlib.sha256(
        f"{session_scope_sha256}:{digest}".encode("utf-8")
    ).hexdigest()
    return (
        f"session-public-fact:{scoped_digest[:32]}",
        f"sha256:{digest}",
    )


def session_public_fact_integrity_valid(fact: SessionPublicFact) -> bool:
    """Recompute both content and session bindings from server-visible fields."""

    try:
        finding = ResearchFinding(
            variable_key=fact.variable_key,
            effective_year=fact.effective_year,
            value=fact.value,
            unit=fact.unit,
            jurisdiction=fact.jurisdiction,
            sources=fact.sources,
        )
        expected_id, expected_content = session_public_fact_identity(
            finding,
            session_scope_sha256=fact.session_scope_sha256,
        )
    except Exception:
        return False
    return hmac.compare_digest(fact.fact_id, expected_id) and hmac.compare_digest(
        fact.content_sha256,
        expected_content,
    )


class ResearchGateway(Protocol):
    """Read-only boundary implemented by the hosted web-search gateway."""

    def find(
        self,
        request: ResearchRequest,
        *,
        rule: ResearchRule,
    ) -> ResearchFinding | None: ...


class ResearchRuleRegistry:
    def __init__(self, rules: tuple[ResearchRule, ...]) -> None:
        by_variable: dict[str, ResearchRule] = {}
        for rule in rules:
            if rule.variable_key in by_variable:
                raise ValueError(
                    f"duplicate research rule: {rule.variable_key}"
                )
            by_variable[rule.variable_key] = rule
        self._by_variable = by_variable

    def get(self, variable_key: str) -> ResearchRule | None:
        return self._by_variable.get(str(variable_key or "").strip())


def build_default_research_rule_registry() -> ResearchRuleRegistry:
    """Return the narrow set with both an authority and a value validator."""

    return ResearchRuleRegistry(
        (
            ResearchRule(
                variable_key="federal_standard_deduction",
                label="U.S. federal standard deduction by filing status",
                expected_unit="USD_by_filing_status",
                jurisdiction="US",
                allowed_domains=("irs.gov",),
                allowed_publishers=("Internal Revenue Service",),
            ),
            ResearchRule(
                variable_key="federal_tax_brackets",
                label="U.S. federal income tax brackets",
                expected_unit="USD_thresholds_and_percent_rates",
                jurisdiction="US",
                allowed_domains=("irs.gov",),
                allowed_publishers=("Internal Revenue Service",),
            ),
            ResearchRule(
                variable_key="retirement_contribution_limits",
                label="U.S. 401(k), IRA, and HSA contribution limits",
                expected_unit="USD_annual_limits",
                jurisdiction="US",
                allowed_domains=("irs.gov",),
                allowed_publishers=("Internal Revenue Service",),
            ),
            ResearchRule(
                variable_key="social_security_cola",
                label="U.S. Social Security cost-of-living adjustment",
                expected_unit="percent",
                jurisdiction="US",
                allowed_domains=("ssa.gov",),
                allowed_publishers=("Social Security Administration",),
            ),
            ResearchRule(
                variable_key="social_security_taxable_maximum",
                label="U.S. Social Security taxable maximum",
                expected_unit="USD_annual",
                jurisdiction="US",
                allowed_domains=("ssa.gov",),
                allowed_publishers=("Social Security Administration",),
            ),
            ResearchRule(
                variable_key="medicare_part_b_premium",
                label="U.S. standard Medicare Part B monthly premium",
                expected_unit="USD_per_month",
                jurisdiction="US",
                allowed_domains=("cms.gov",),
                allowed_publishers=(
                    "Centers for Medicare & Medicaid Services",
                ),
            ),
        )
    )


class InMemoryResearchAttemptLedger:
    """Process-local circuit breaker for failed online research attempts."""

    def __init__(self, *, cooldown: timedelta = timedelta(minutes=15)) -> None:
        self.cooldown = cooldown
        self._lock = RLock()
        self._attempted_at: dict[tuple[str, int], datetime] = {}

    def recently_attempted(
        self,
        variable_key: str,
        effective_year: int,
        *,
        as_of: datetime,
    ) -> bool:
        with self._lock:
            attempted_at = self._attempted_at.get(
                (variable_key, effective_year)
            )
            return (
                attempted_at is not None
                and as_of - attempted_at < self.cooldown
            )

    def record(
        self,
        variable_key: str,
        effective_year: int,
        *,
        attempted_at: datetime,
    ) -> None:
        with self._lock:
            self._attempted_at[(variable_key, effective_year)] = attempted_at

    def claim(
        self,
        variable_key: str,
        effective_year: int,
        *,
        attempted_at: datetime,
    ) -> bool:
        """Atomically reserve the single permitted attempt in the cooldown."""

        with self._lock:
            prior = self._attempted_at.get((variable_key, effective_year))
            if prior is not None and attempted_at - prior < self.cooldown:
                return False
            self._attempted_at[(variable_key, effective_year)] = attempted_at
            return True


class ResearchSpecialist:
    """Policy-gated research for session facts or unapproved candidates."""

    provider_id = "research-specialist"

    def __init__(
        self,
        *,
        gateway: ResearchGateway,
        repository: GovernedAssumptionRepository | None,
        validators: AuthoritativeProviderRegistry,
        source_policy: VariableSourceRegistry | None = None,
        rules: ResearchRuleRegistry | None = None,
        attempt_ledger: InMemoryResearchAttemptLedger | None = None,
    ) -> None:
        self.gateway = gateway
        self.repository = repository
        self.validators = validators
        self.source_policy = source_policy or load_variable_source_registry()
        self.rules = rules or build_default_research_rule_registry()
        self.attempt_ledger = (
            attempt_ledger or InMemoryResearchAttemptLedger()
        )

    def collect_candidate(
        self,
        request: ResearchRequest,
        *,
        retrieved_at: datetime | None = None,
    ) -> ProviderCandidateBatch:
        retrieved_at = retrieved_at or datetime.now(timezone.utc)
        if retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        if self.repository is None:
            raise self._error(
                ResearchErrorCode.PERSISTENCE_FAILED,
                "research candidate persistence is not configured",
                request=request,
                attempted=False,
            )
        finding = self._collect_validated_finding(
            request,
            retrieved_at=retrieved_at,
            session_use=False,
            claim_attempt=True,
        )

        batch = self._candidate_batch(
            finding,
            request=request,
            retrieved_at=retrieved_at,
        )
        try:
            self.repository.save_batch(batch)
        except Exception as exc:
            raise self._error(
                ResearchErrorCode.PERSISTENCE_FAILED,
                "researched candidate could not be persisted",
                request=request,
                attempted=True,
            ) from exc
        return batch

    def collect_session_fact(
        self,
        request: ResearchRequest,
        *,
        session_scope_sha256: str,
        retrieved_at: datetime | None = None,
    ) -> SessionPublicFact:
        """Return one validated fact without durable persistence or approval."""

        retrieved_at = retrieved_at or datetime.now(timezone.utc)
        if retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", session_scope_sha256):
            raise ValueError("session_scope_sha256 is invalid")
        finding = self._current_approved_finding(
            request,
            retrieved_at=retrieved_at,
        )
        origin: Literal["live_research", "durable_registry"] = (
            "durable_registry" if finding is not None else "live_research"
        )
        if finding is None:
            finding = self._collect_validated_finding(
                request,
                retrieved_at=retrieved_at,
                session_use=True,
                claim_attempt=False,
            )
        fact_id, content_sha256 = session_public_fact_identity(
            finding,
            session_scope_sha256=session_scope_sha256,
        )
        value = finding.value
        calculation_allowed = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and finding.unit in {"USD_annual", "USD_per_month", "percent"}
        )
        return SessionPublicFact(
            fact_id=fact_id,
            content_sha256=content_sha256,
            session_scope_sha256=session_scope_sha256,
            variable_key=finding.variable_key,
            effective_year=finding.effective_year,
            value=finding.value,
            unit=finding.unit,
            jurisdiction=finding.jurisdiction,
            sources=finding.sources,
            retrieved_at=retrieved_at,
            expires_at=retrieved_at + SESSION_PUBLIC_FACT_TTL,
            origin=origin,
            session_calculation_allowed=calculation_allowed,
        )

    def _current_approved_finding(
        self,
        request: ResearchRequest,
        *,
        retrieved_at: datetime,
    ) -> ResearchFinding | None:
        """Resolve a current durable value before opening the live research path."""

        if self.repository is None or not hasattr(
            self.repository,
            "latest_approved",
        ):
            return None
        policy = self.source_policy.get(request.variable_key)
        rule = self.rules.get(request.variable_key)
        adapter = self.validators.provider_for_variable(request.variable_key)
        if (
            policy is None
            or rule is None
            or adapter is None
            or policy.source_class is not SourceClass.PUBLIC_AUTHORITATIVE
            or PermittedUse.REPORTING not in policy.allowed_uses
        ):
            return None
        approved = self.repository.latest_approved(
            request.variable_key,
            effective_year=request.effective_year,
        )
        if (
            approved is None
            or approved.status is not AssumptionStatus.APPROVED
            or approved.source_class is not SourceClass.PUBLIC_AUTHORITATIVE
            or PermittedUse.REPORTING not in approved.permitted_uses
            or approved.effective_from is None
            or approved.effective_to is None
            or approved.effective_from.year != request.effective_year
            or not approved.evidence
        ):
            return None
        latest_retrieval = max(
            evidence.retrieved_at for evidence in approved.evidence
        )
        if (
            latest_retrieval.tzinfo is None
            or latest_retrieval > retrieved_at
            or (
                policy.maximum_age_days is not None
                and retrieved_at - latest_retrieval
                > timedelta(days=policy.maximum_age_days)
            )
        ):
            return None
        try:
            verified = adapter.collect_candidates(
                ProviderRequest(
                    effective_year=request.effective_year,
                    variable_keys=(request.variable_key,),
                ),
                retrieved_at=retrieved_at,
            ).artifacts[0]
        except Exception:
            return None
        if (
            verified.value != approved.value
            or verified.unit != approved.unit
            or verified.jurisdiction != approved.jurisdiction
        ):
            return None
        try:
            finding = ResearchFinding(
                variable_key=approved.variable_key,
                effective_year=request.effective_year,
                value=approved.value,
                unit=approved.unit,
                jurisdiction=str(approved.jurisdiction or ""),
                sources=tuple(
                    ResearchSource(
                        publisher=evidence.publisher,
                        title=evidence.title,
                        url=str(evidence.url or ""),
                        published_at=evidence.published_at,
                    )
                    for evidence in approved.evidence[:3]
                ),
            )
            self._validate_finding(
                finding,
                request=request,
                rule=rule,
                retrieved_at=retrieved_at,
            )
            adapter.validate_value(request.variable_key, finding.value)
        except Exception:
            return None
        return finding

    def _collect_validated_finding(
        self,
        request: ResearchRequest,
        *,
        retrieved_at: datetime,
        session_use: bool,
        claim_attempt: bool,
    ) -> ResearchFinding:
        policy = self.source_policy.get(request.variable_key)
        policy_allows_research = bool(
            policy is not None
            and policy.source_class is SourceClass.PUBLIC_AUTHORITATIVE
            and policy.online_research_allowed
        )
        policy_allows_use = bool(
            policy_allows_research
            and policy is not None
            and (
                (
                    policy.session_use_allowed
                    and PermittedUse.REPORTING in policy.allowed_uses
                )
                if session_use
                else policy.approval_required
            )
        )
        if not policy_allows_use:
            raise self._error(
                ResearchErrorCode.POLICY_DENIED,
                "variable policy does not permit this governed research use",
                request=request,
                attempted=False,
            )

        rule = self.rules.get(request.variable_key)
        adapter = self.validators.provider_for_variable(request.variable_key)
        if rule is None or adapter is None:
            raise self._error(
                ResearchErrorCode.RULE_NOT_CONFIGURED,
                "no constrained research rule and deterministic validator exist",
                request=request,
                attempted=False,
            )
        if claim_attempt and not self.attempt_ledger.claim(
            request.variable_key,
            request.effective_year,
            attempted_at=retrieved_at,
        ):
            raise self._error(
                ResearchErrorCode.RECENT_ATTEMPT,
                "research was already attempted recently for this variable",
                request=request,
                attempted=False,
            )

        try:
            finding = self.gateway.find(request, rule=rule)
        except ResearchSpecialistError:
            raise
        except Exception as exc:
            raise self._error(
                ResearchErrorCode.GATEWAY_UNAVAILABLE,
                "research gateway failed",
                request=request,
                attempted=True,
            ) from exc
        if finding is None:
            raise self._error(
                ResearchErrorCode.NO_RESULT,
                "no authoritative value was found",
                request=request,
                attempted=True,
            )

        self._validate_finding(
            finding,
            request=request,
            rule=rule,
            retrieved_at=retrieved_at,
        )
        try:
            adapter.validate_value(request.variable_key, finding.value)
        except ProviderAdapterError as exc:
            raise self._error(
                ResearchErrorCode.VALUE_INVALID,
                "researched value failed the deterministic provider validator",
                request=request,
                attempted=True,
            ) from exc
        return finding

    def _validate_finding(
        self,
        finding: ResearchFinding,
        *,
        request: ResearchRequest,
        rule: ResearchRule,
        retrieved_at: datetime,
    ) -> None:
        if (
            finding.variable_key != request.variable_key
            or finding.effective_year != request.effective_year
            or finding.unit != rule.expected_unit
            or finding.jurisdiction != rule.jurisdiction
        ):
            raise self._error(
                ResearchErrorCode.OUTPUT_INVALID,
                "research output does not match the requested variable contract",
                request=request,
                attempted=True,
            )
        for source in finding.sources:
            if (
                source.published_at is not None
                and source.published_at > retrieved_at
            ):
                raise self._error(
                    ResearchErrorCode.OUTPUT_INVALID,
                    "research source publication time is in the future",
                    request=request,
                    attempted=True,
                )
            if source.publisher not in rule.allowed_publishers:
                raise self._error(
                    ResearchErrorCode.SOURCE_NOT_ALLOWED,
                    "research publisher is not allowlisted",
                    request=request,
                    attempted=True,
                )
            parsed = urlparse(source.url)
            hostname = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not any(
                hostname == domain
                or hostname.endswith(f".{domain}")
                for domain in rule.allowed_domains
            ):
                raise self._error(
                    ResearchErrorCode.SOURCE_NOT_ALLOWED,
                    "research source is not an allowlisted HTTPS authority",
                    request=request,
                    attempted=True,
                )

    def _candidate_batch(
        self,
        finding: ResearchFinding,
        *,
        request: ResearchRequest,
        retrieved_at: datetime,
    ) -> ProviderCandidateBatch:
        digest = research_finding_content_sha256(finding)
        effective_from = datetime(
            request.effective_year,
            1,
            1,
            tzinfo=timezone.utc,
        )
        effective_to = datetime(
            request.effective_year + 1,
            1,
            1,
            tzinfo=timezone.utc,
        ) - timedelta(microseconds=1)
        evidence = tuple(
            AssumptionEvidence(
                source_id=(
                    f"research:{request.variable_key}:"
                    f"{index + 1}:{digest[:12]}"
                ),
                source_type="authoritative_web_research",
                publisher=source.publisher,
                title=source.title,
                url=source.url,
                published_at=source.published_at,
                retrieved_at=retrieved_at,
                content_hash=digest,
            )
            for index, source in enumerate(finding.sources)
        )
        candidate = AssumptionArtifact(
            artifact_id=(
                f"candidate:{self.provider_id}:{request.effective_year}:"
                f"{request.variable_key}:{digest[:16]}"
            ),
            variable_key=request.variable_key,
            source_class=SourceClass.PUBLIC_AUTHORITATIVE,
            value=finding.value,
            unit=finding.unit,
            jurisdiction=finding.jurisdiction,
            effective_from=effective_from,
            effective_to=effective_to,
            status=AssumptionStatus.CANDIDATE,
            permitted_uses=(PermittedUse.REPORTING,),
            evidence=evidence,
            created_at=retrieved_at,
        )
        return ProviderCandidateBatch(
            provider_id=self.provider_id,
            effective_year=request.effective_year,
            snapshot_sha256=digest,
            retrieved_at=retrieved_at,
            artifacts=(candidate,),
        )

    @staticmethod
    def _error(
        code: ResearchErrorCode,
        message: str,
        *,
        request: ResearchRequest,
        attempted: bool,
    ) -> ResearchSpecialistError:
        return ResearchSpecialistError(
            code,
            message,
            variable_key=request.variable_key,
            effective_year=request.effective_year,
            attempted=attempted,
        )


__all__ = [
    "InMemoryResearchAttemptLedger",
    "ResearchErrorCode",
    "ResearchFinding",
    "ResearchGateway",
    "ResearchRequest",
    "ResearchRule",
    "ResearchRuleRegistry",
    "ResearchSource",
    "ResearchSpecialist",
    "ResearchSpecialistError",
    "SessionPublicFact",
    "build_default_research_rule_registry",
    "research_finding_content_sha256",
    "session_public_fact_identity",
    "session_public_fact_integrity_valid",
]

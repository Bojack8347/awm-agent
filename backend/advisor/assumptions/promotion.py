"""Narrow server-owned promotion of independently verified public facts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlparse

from advisor.assumptions.contracts import (
    AssumptionArtifact,
    DurableFactAgentAssessment,
    DurableFactPromotionCandidate,
    DurableFactPromotionChecks,
    DurableFactPromotionDecision,
    DurableFactPromotionExamination,
    DurableFactPromotionExpectedActive,
    DurableFactPromotionPolicy,
    DurableFactPromotionVerification,
    PermittedUse,
    SourceClass,
)
from advisor.assumptions.governance import (
    AssumptionReviewRequest,
    GovernedAssumptionRepository,
    GovernanceDecision,
    assumption_artifact_fingerprint,
    build_approved_assumption,
)
from advisor.assumptions.providers.contracts import (
    ProviderAdapterError,
    ProviderCandidateBatch,
    ProviderRequest,
)
from advisor.assumptions.providers.registry import (
    AuthoritativeProviderRegistry,
)
from advisor.assumptions.research import (
    ResearchFinding,
    ResearchRule,
    ResearchRuleRegistry,
    SessionPublicFact,
    build_default_research_rule_registry,
    research_finding_content_sha256,
    session_public_fact_integrity_valid,
)
from advisor.assumptions.registry import (
    VariableSourceRegistry,
    load_variable_source_registry,
)


SERVER_PROMOTION_ACTOR = "server:verified-public-fact-promotion:v1"
AGENT_PROMOTION_ACTOR = "agent:public-fact-reuse-reviewer:v1"
VERIFIER_USE_CEILING = (
    PermittedUse.REPORTING,
    PermittedUse.MODEL_INPUT,
)


def build_durable_fact_agent_assessment(
    fact: SessionPublicFact,
    *,
    decision: Literal[
        "authorize_durable_reuse",
        "keep_session_only",
    ],
    reason_code: Literal[
        "authoritative_fact_reuse_appropriate",
        "authoritative_fact_reuse_not_appropriate",
    ],
    assessed_at: datetime | None = None,
) -> DurableFactAgentAssessment:
    """Stamp one bounded agent decision onto the exact researched fact."""

    fact = SessionPublicFact.model_validate(fact)
    if not session_public_fact_integrity_valid(fact):
        raise ValueError("cannot assess a session fact with invalid integrity")
    assessed_at = assessed_at or datetime.now(timezone.utc)
    if assessed_at.tzinfo is None:
        raise ValueError("assessed_at must be timezone-aware")
    if not fact.retrieved_at <= assessed_at < fact.expires_at:
        raise ValueError("agent assessment must occur during the session fact window")
    identity_payload = {
        "assessed_by": AGENT_PROMOTION_ACTOR,
        "assessed_at": assessed_at.isoformat(),
        "finding_content_sha256": fact.content_sha256,
        "decision": decision,
        "reason_code": reason_code,
    }
    digest = hashlib.sha256(_canonical_json(identity_payload)).hexdigest()
    return DurableFactAgentAssessment(
        assessment_id=f"durable-fact-agent-assessment:{digest[:32]}",
        assessed_by=AGENT_PROMOTION_ACTOR,
        assessed_at=assessed_at,
        finding_content_sha256=fact.content_sha256,
        decision=decision,
        reason_code=reason_code,
    )


class AutomaticPublicFactPromotionService:
    """Examine and promote only a provider-snapshot match; never route a question."""

    def __init__(
        self,
        *,
        repository: GovernedAssumptionRepository,
        providers: AuthoritativeProviderRegistry,
        source_policy: VariableSourceRegistry | None = None,
        rules: ResearchRuleRegistry | None = None,
    ) -> None:
        self.repository = repository
        self.providers = providers
        self.source_policy = source_policy or load_variable_source_registry()
        self.rules = rules or build_default_research_rule_registry()

    def examine_and_promote(
        self,
        fact: SessionPublicFact,
        *,
        assessment: DurableFactAgentAssessment | None = None,
        examined_at: datetime | None = None,
    ) -> DurableFactPromotionExamination:
        """Apply mechanical gates to an agent-reviewed durable reuse decision."""

        fact = SessionPublicFact.model_validate(fact)
        examined_at = examined_at or datetime.now(timezone.utc)
        if examined_at.tzinfo is None:
            raise ValueError("examined_at must be timezone-aware")

        finding = ResearchFinding(
            variable_key=fact.variable_key,
            effective_year=fact.effective_year,
            value=fact.value,
            unit=fact.unit,
            jurisdiction=fact.jurisdiction,
            sources=fact.sources,
        )
        finding_digest = research_finding_content_sha256(finding)
        candidate_value_digest = _value_sha256(fact.value)
        assessment_error: str | None = None
        assessment_model: DurableFactAgentAssessment | None = None
        if assessment is None:
            assessment_error = "agent_assessment_required"
        else:
            try:
                assessment_model = DurableFactAgentAssessment.model_validate(
                    assessment
                )
            except Exception:
                assessment_error = "agent_assessment_invalid"
            else:
                if (
                    assessment_model.assessed_by != AGENT_PROMOTION_ACTOR
                    or assessment_model.finding_content_sha256
                    != fact.content_sha256
                    or not (
                        fact.retrieved_at
                        <= assessment_model.assessed_at
                        <= examined_at
                    )
                ):
                    assessment_model = None
                    assessment_error = "agent_assessment_invalid"
                elif assessment_model.decision == "keep_session_only":
                    assessment_error = "agent_assessment_kept_session_only"
        policy = self.source_policy.get(fact.variable_key)
        rule = self.rules.get(fact.variable_key)
        adapter = self.providers.provider_for_variable(fact.variable_key)

        session_integrity = session_public_fact_integrity_valid(fact)
        session_current = bool(
            fact.retrieved_at <= examined_at < fact.expires_at
        )
        policy_authorized = bool(
            policy is not None
            and policy.source_class is SourceClass.PUBLIC_AUTHORITATIVE
            and policy.automatic_promotion_allowed
            and PermittedUse.REPORTING in policy.allowed_uses
            and PermittedUse.MODEL_INPUT in policy.allowed_uses
        )
        source_authority = bool(
            rule is not None
            and _sources_match_rule(fact, rule=rule, examined_at=examined_at)
        )
        dimensional_contract = bool(
            rule is not None
            and fact.unit == rule.expected_unit
            and fact.jurisdiction == rule.jurisdiction
        )
        value_shape = False
        if adapter is not None:
            try:
                adapter.validate_value(fact.variable_key, fact.value)
            except ProviderAdapterError:
                pass
            else:
                value_shape = True

        persistence_available = False
        try:
            persistence_available = bool(
                self.repository.durable_promotion_available()
            )
        except Exception:
            persistence_available = False

        batch: ProviderCandidateBatch | None = None
        verified: AssumptionArtifact | None = None
        verification: DurableFactPromotionVerification | None = None
        independent_match = False
        verification_error: str | None = None
        if adapter is None:
            verification_error = "independent_verifier_unavailable"
        else:
            try:
                batch = adapter.collect_candidates(
                    ProviderRequest(
                        effective_year=fact.effective_year,
                        variable_keys=(fact.variable_key,),
                    ),
                    retrieved_at=examined_at,
                )
                verified = batch.artifacts[0]
                verified_value_digest = _value_sha256(verified.value)
                verification = DurableFactPromotionVerification(
                    provider_id=adapter.provider_id,
                    verifier_version="1",
                    source_snapshot_sha256=batch.snapshot_sha256,
                    canonical_value_sha256=verified_value_digest,
                    verified_at=examined_at,
                    evidence=verified.evidence,
                )
                independent_match = bool(
                    verified.variable_key == fact.variable_key
                    and verified.effective_from is not None
                    and verified.effective_from.year == fact.effective_year
                    and verified.value == fact.value
                    and verified_value_digest == candidate_value_digest
                    and verified.unit == fact.unit
                    and verified.jurisdiction == fact.jurisdiction
                    and _verification_is_fresh(
                        verified,
                        maximum_age_days=(
                            policy.maximum_age_days if policy is not None else None
                        ),
                        examined_at=examined_at,
                    )
                )
                if not independent_match:
                    verification_error = "independent_value_mismatch"
            except ProviderAdapterError:
                verification_error = "independent_verification_unavailable"
            except Exception:
                verification_error = "independent_verification_invalid"

        granted_uses = tuple(
            use
            for use in VERIFIER_USE_CEILING
            if policy is not None and use in policy.allowed_uses
        )
        promotion_policy = (
            DurableFactPromotionPolicy(
                policy_id=self.source_policy.document.policy_id,
                policy_version=self.source_policy.document.policy_version,
                automatic_promotion_allowed=policy.automatic_promotion_allowed,
                maximum_age_days=policy.maximum_age_days,
                granted_uses=granted_uses if policy_authorized else (),
            )
            if policy is not None
            else None
        )
        current = self.repository.latest_approved(
            fact.variable_key,
            effective_year=fact.effective_year,
        )
        expected_active = _expected_active(current)
        candidate_contract = _candidate_contract(
            fact=fact,
            finding_digest=finding_digest,
            value_digest=candidate_value_digest,
            verified=verified,
        )
        checks = DurableFactPromotionChecks(
            session_integrity=session_integrity,
            session_current=session_current,
            policy_authorized=policy_authorized,
            source_authority=source_authority,
            dimensional_contract=dimensional_contract,
            value_shape=value_shape,
            independent_value_match=independent_match,
            durable_persistence=persistence_available,
            active_state_unchanged=True,
        )
        refusal_reasons = _failed_check_reasons(checks)
        if verification_error is not None:
            refusal_reasons.append(verification_error)
        if assessment_error is not None:
            refusal_reasons.append(assessment_error)
        refusal_reasons = list(dict.fromkeys(refusal_reasons))
        if refusal_reasons or verified is None or batch is None or promotion_policy is None:
            return _examination(
                examined_at=examined_at,
                candidate=candidate_contract,
                verification=verification,
                agent_assessment=assessment_model,
                policy=(
                    promotion_policy.model_copy(update={"granted_uses": ()})
                    if promotion_policy is not None
                    else None
                ),
                expected_active=expected_active,
                checks=checks,
                decision=DurableFactPromotionDecision(
                    status="session_only",
                    reason_codes=tuple(refusal_reasons or ("promotion_not_eligible",)),
                ),
            )

        verified_fingerprint = assumption_artifact_fingerprint(verified)
        if (
            current is not None
            and _source_content_fingerprint(current)
            == _source_content_fingerprint(verified)
        ):
            actual_uses = tuple(
                use for use in granted_uses if use in current.permitted_uses
            )
            current_policy = promotion_policy.model_copy(
                update={"granted_uses": actual_uses}
            )
            return _examination(
                examined_at=examined_at,
                candidate=candidate_contract,
                verification=verification,
                agent_assessment=assessment_model,
                policy=current_policy,
                expected_active=expected_active,
                checks=checks,
                decision=DurableFactPromotionDecision(
                    status="already_current",
                    approved_artifact_id=current.artifact_id,
                    approved_version=current.assumption_set_version,
                ),
            )

        if current is not None:
            return _examination(
                examined_at=examined_at,
                candidate=candidate_contract,
                verification=verification,
                agent_assessment=assessment_model,
                policy=promotion_policy.model_copy(update={"granted_uses": ()}),
                expected_active=expected_active,
                checks=checks,
                decision=DurableFactPromotionDecision(
                    status="session_only",
                    reason_codes=("active_durable_value_conflict",),
                ),
            )

        next_version = 1
        predicted = build_approved_assumption(
            candidate=verified,
            reviewer_id=AGENT_PROMOTION_ACTOR,
            decided_at=examined_at,
            approved_version=next_version,
            approved_uses=granted_uses,
        )
        promoted_examination = _examination(
            examined_at=examined_at,
            candidate=candidate_contract,
            verification=verification,
            agent_assessment=assessment_model,
            policy=promotion_policy,
            expected_active=expected_active,
            checks=checks,
            decision=DurableFactPromotionDecision(
                status="promoted",
                approved_artifact_id=predicted.artifact_id,
                approved_version=predicted.assumption_set_version,
            ),
        )
        request = AssumptionReviewRequest(
            candidate_artifact_id=verified.artifact_id,
            expected_fingerprint=verified_fingerprint,
            decision=GovernanceDecision.APPROVE,
            reason=(
                "agent authorized durable reuse after mechanical verification"
            ),
            idempotency_key=promoted_examination.idempotency_key,
        )
        try:
            decision = self.repository.apply_verified_promotion(
                batch=batch,
                request=request,
                reviewer_id=AGENT_PROMOTION_ACTOR,
                decided_at=examined_at,
                policy_id=promotion_policy.policy_id,
                policy_version=promotion_policy.policy_version,
                approved_uses=granted_uses,
                promotion_examination=promoted_examination,
            )
            persisted_examination = decision.promotion_examination
            if (
                decision.approved_artifact_id != predicted.artifact_id
                or decision.approved_version != predicted.assumption_set_version
                or persisted_examination is None
                or persisted_examination.examination_id
                != promoted_examination.examination_id
            ):
                raise RuntimeError(
                    "promotion persistence returned mismatched lineage"
                )
            return persisted_examination
        except Exception:
            latest = self.repository.latest_approved(
                fact.variable_key,
                effective_year=fact.effective_year,
            )
            if (
                latest is not None
                and _source_content_fingerprint(latest)
                == _source_content_fingerprint(verified)
            ):
                latest_expected = _expected_active(latest)
                return _examination(
                    examined_at=examined_at,
                    candidate=candidate_contract,
                    verification=verification,
                    agent_assessment=assessment_model,
                    policy=promotion_policy.model_copy(
                        update={
                            "granted_uses": tuple(
                                use
                                for use in granted_uses
                                if use in latest.permitted_uses
                            )
                        }
                    ),
                    expected_active=latest_expected,
                    checks=checks,
                    decision=DurableFactPromotionDecision(
                        status="already_current",
                        approved_artifact_id=latest.artifact_id,
                        approved_version=latest.assumption_set_version,
                    ),
                )
            failed_checks = checks.model_copy(
                update={"active_state_unchanged": False}
            )
            return _examination(
                examined_at=examined_at,
                candidate=candidate_contract,
                verification=verification,
                agent_assessment=assessment_model,
                policy=promotion_policy.model_copy(update={"granted_uses": ()}),
                expected_active=expected_active,
                checks=failed_checks,
                decision=DurableFactPromotionDecision(
                    status="session_only",
                    reason_codes=("durable_promotion_commit_failed",),
                ),
            )

def _candidate_contract(
    *,
    fact: SessionPublicFact,
    finding_digest: str,
    value_digest: str,
    verified: AssumptionArtifact | None,
) -> DurableFactPromotionCandidate:
    effective_from, effective_to = _effective_window(fact.effective_year)
    return DurableFactPromotionCandidate(
        artifact_id=verified.artifact_id if verified is not None else None,
        artifact_fingerprint=(
            assumption_artifact_fingerprint(verified)
            if verified is not None
            else None
        ),
        finding_content_sha256=f"sha256:{finding_digest}",
        canonical_value_sha256=value_digest,
        variable_key=fact.variable_key,
        effective_year=fact.effective_year,
        effective_from=effective_from,
        effective_to=effective_to,
        unit=fact.unit,
        jurisdiction=fact.jurisdiction,
        researched_at=fact.retrieved_at,
        session_expires_at=fact.expires_at,
    )


def _expected_active(
    artifact: AssumptionArtifact | None,
) -> DurableFactPromotionExpectedActive:
    if artifact is None:
        return DurableFactPromotionExpectedActive()
    return DurableFactPromotionExpectedActive(
        artifact_id=artifact.artifact_id,
        artifact_version=artifact.assumption_set_version,
        artifact_fingerprint=assumption_artifact_fingerprint(artifact),
    )


def _examination(
    *,
    examined_at: datetime,
    candidate: DurableFactPromotionCandidate,
    verification: DurableFactPromotionVerification | None,
    agent_assessment: DurableFactAgentAssessment | None,
    policy: DurableFactPromotionPolicy | None,
    expected_active: DurableFactPromotionExpectedActive,
    checks: DurableFactPromotionChecks,
    decision: DurableFactPromotionDecision,
) -> DurableFactPromotionExamination:
    identity_payload = {
        "candidate": {
            "artifact_id": candidate.artifact_id,
            "artifact_fingerprint": candidate.artifact_fingerprint,
            "finding_content_sha256": candidate.finding_content_sha256,
            "canonical_value_sha256": candidate.canonical_value_sha256,
            "variable_key": candidate.variable_key,
            "effective_year": candidate.effective_year,
            "unit": candidate.unit,
            "jurisdiction": candidate.jurisdiction,
        },
        "verification": (
            {
                "profile": verification.profile,
                "provider_id": verification.provider_id,
                "verifier_version": verification.verifier_version,
                "source_snapshot_sha256": verification.source_snapshot_sha256,
                "canonical_value_sha256": verification.canonical_value_sha256,
            }
            if verification
            else None
        ),
        "agent_assessment": (
            agent_assessment.model_dump(mode="json")
            if agent_assessment is not None
            else None
        ),
        "policy": policy.model_dump(mode="json") if policy else None,
        "expected_active": expected_active.model_dump(mode="json"),
        "decision": decision.model_dump(mode="json"),
    }
    digest = hashlib.sha256(_canonical_json(identity_payload)).hexdigest()
    return DurableFactPromotionExamination(
        schema_version="awm.durable_fact_promotion.v2",
        examination_id=f"durable-fact-examination:{digest[:32]}",
        idempotency_key=f"agent-reviewed-public-fact-promotion:{digest}",
        examined_at=examined_at,
        candidate=candidate,
        verification=verification,
        agent_assessment=agent_assessment,
        policy=policy,
        expected_active=expected_active,
        checks=checks,
        decision=decision,
    )


def _failed_check_reasons(checks: DurableFactPromotionChecks) -> list[str]:
    mapping = {
        "session_integrity": "session_fact_integrity_invalid",
        "session_current": "session_fact_not_current",
        "policy_authorized": "automatic_promotion_not_authorized",
        "source_authority": "research_source_not_authoritative",
        "dimensional_contract": "research_dimension_mismatch",
        "value_shape": "research_value_shape_invalid",
        "independent_value_match": "independent_value_mismatch",
        "durable_persistence": "durable_persistence_unavailable",
        "active_state_unchanged": "durable_state_changed",
    }
    return [
        reason
        for field, reason in mapping.items()
        if getattr(checks, field) is not True
    ]


def _sources_match_rule(
    fact: SessionPublicFact,
    *,
    rule: ResearchRule,
    examined_at: datetime,
) -> bool:
    if not fact.sources:
        return False
    for source in fact.sources:
        parsed = urlparse(source.url)
        hostname = (parsed.hostname or "").lower()
        if (
            source.publisher not in rule.allowed_publishers
            or parsed.scheme != "https"
            or not any(
                hostname == domain or hostname.endswith(f".{domain}")
                for domain in rule.allowed_domains
            )
            or (
                source.published_at is not None
                and source.published_at > examined_at
            )
        ):
            return False
    return True


def _verification_is_fresh(
    artifact: AssumptionArtifact,
    *,
    maximum_age_days: int | None,
    examined_at: datetime,
) -> bool:
    if not artifact.evidence:
        return False
    for evidence in artifact.evidence:
        if evidence.retrieved_at.tzinfo is None or evidence.retrieved_at > examined_at:
            return False
        if evidence.published_at is not None:
            if evidence.published_at.tzinfo is None or evidence.published_at > examined_at:
                return False
            if (
                maximum_age_days is not None
                and examined_at - evidence.published_at
                > timedelta(days=maximum_age_days)
            ):
                return False
    return True


def _effective_window(effective_year: int) -> tuple[datetime, datetime]:
    start = datetime(effective_year, 1, 1, tzinfo=timezone.utc)
    end = datetime(effective_year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(
        microseconds=1
    )
    return start, end


def _value_sha256(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}"


def _source_content_fingerprint(artifact: AssumptionArtifact) -> str:
    payload = artifact.model_dump(mode="json")
    for key in (
        "artifact_id",
        "created_at",
        "status",
        "permitted_uses",
        "approved_by",
        "approved_at",
        "assumption_set_id",
        "assumption_set_version",
    ):
        payload.pop(key, None)
    for evidence in payload.get("evidence", []):
        evidence.pop("retrieved_at", None)
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


__all__ = [
    "AGENT_PROMOTION_ACTOR",
    "AutomaticPublicFactPromotionService",
    "SERVER_PROMOTION_ACTOR",
    "VERIFIER_USE_CEILING",
    "build_durable_fact_agent_assessment",
]

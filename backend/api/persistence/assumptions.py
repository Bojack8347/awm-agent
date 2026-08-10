"""PostgreSQL persistence for governed public-authoritative assumptions.

This module implements the advisor-layer repository protocol without changing
the canonical API persistence facade. In ``auto`` or ``off`` database modes it
uses the same process-local fallback contract.
"""

from __future__ import annotations

import json
from datetime import datetime
from threading import RLock
from typing import Any

from advisor.assumptions.contracts import (
    AssumptionArtifact,
    AssumptionEvidence,
    AssumptionStatus,
    DurableFactPromotionExamination,
    PermittedUse,
)
from advisor.assumptions.governance import (
    AssumptionCandidateReview,
    AssumptionDecisionRecord,
    AssumptionGovernanceError,
    AssumptionReviewRequest,
    GovernanceDecision,
    GovernanceErrorCode,
    assumption_artifact_fingerprint,
    build_approved_assumption,
    build_assumption_decision_record,
)
from advisor.assumptions.providers.contracts import ProviderCandidateBatch
from advisor.assumptions.providers.governed_repository import (
    InMemoryGovernedAssumptionRepository,
)

from . import core


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _hydrate_artifact(
    raw: Any,
    *,
    governance_status: str | None = None,
    last_verified_at: datetime | None = None,
) -> AssumptionArtifact:
    artifact = AssumptionArtifact.model_validate(_json_value(raw))
    if last_verified_at is not None and artifact.evidence:
        artifact = artifact.model_copy(
            update={
                "evidence": tuple(
                    AssumptionEvidence.model_validate(
                        {
                            **evidence.model_dump(),
                            "retrieved_at": last_verified_at,
                        }
                    )
                    for evidence in artifact.evidence
                )
            }
        )
    if governance_status == "rejected":
        artifact = artifact.model_copy(
            update={"status": AssumptionStatus.REJECTED}
        )
    elif governance_status == "superseded":
        artifact = artifact.model_copy(
            update={"status": AssumptionStatus.SUPERSEDED}
        )
    return artifact


class PostgresAssumptionRepository:
    """Durable repository with explicit process-local development fallback."""

    def __init__(
        self,
        *,
        fallback: InMemoryGovernedAssumptionRepository | None = None,
    ) -> None:
        self.fallback = fallback or InMemoryGovernedAssumptionRepository()

    def _pool_or_none(self):
        return core._get_pool()

    def _handle_failure(self, operation: str, exc: Exception):
        if core.database_mode() == "required":
            raise AssumptionGovernanceError(
                GovernanceErrorCode.PERSISTENCE_UNAVAILABLE,
                f"PostgreSQL assumption {operation} failed",
            ) from exc
        print(
            f"[db] assumption {operation} failed; using process-local fallback",
            flush=True,
        )

    def save_batch(self, batch: ProviderCandidateBatch) -> None:
        pool = self._pool_or_none()
        if pool is None:
            self.fallback.save_batch(batch)
            return
        conn = None
        try:
            conn = core._safe_getconn(pool)
            with conn.cursor() as cur:
                self._insert_candidate_batch(cur, batch)
            conn.commit()
        except Exception as exc:
            if conn is not None:
                conn.rollback()
            if isinstance(exc, AssumptionGovernanceError):
                raise
            self._handle_failure("candidate write", exc)
            self.fallback.save_batch(batch)
        finally:
            if conn is not None:
                pool.putconn(conn)

    @staticmethod
    def _insert_candidate_batch(cur, batch: ProviderCandidateBatch) -> None:
        for artifact in batch.artifacts:
            if artifact.effective_from is None:
                raise ValueError("candidate effective period is required")
            fingerprint = assumption_artifact_fingerprint(artifact)
            cur.execute(
                """
                INSERT INTO assumption_artifacts
                    (artifact_id, variable_key, provider_id,
                     effective_year, source_class, artifact_status,
                     governance_status, content_fingerprint,
                     source_snapshot_sha256, assumption_set_id,
                     approved_version, artifact, created_at,
                     last_verified_at)
                VALUES
                    (%s, %s, %s, %s, %s, 'candidate', 'pending',
                     %s, %s, NULL, NULL, %s::jsonb, %s, %s)
                ON CONFLICT (artifact_id) DO UPDATE SET
                    last_verified_at = GREATEST(
                        assumption_artifacts.last_verified_at,
                        EXCLUDED.last_verified_at
                    ),
                    updated_at = NOW()
                WHERE assumption_artifacts.content_fingerprint =
                      EXCLUDED.content_fingerprint
                """,
                (
                    artifact.artifact_id,
                    artifact.variable_key,
                    batch.provider_id,
                    batch.effective_year,
                    artifact.source_class.value,
                    fingerprint,
                    batch.snapshot_sha256,
                    core._pg_json(artifact.model_dump(mode="json")),
                    artifact.created_at,
                    batch.retrieved_at,
                ),
            )
            cur.execute(
                """
                SELECT content_fingerprint
                FROM assumption_artifacts
                WHERE artifact_id = %s
                """,
                (artifact.artifact_id,),
            )
            row = cur.fetchone()
            if row is None or row[0] != fingerprint:
                raise AssumptionGovernanceError(
                    GovernanceErrorCode.DECISION_CONFLICT,
                    "artifact id conflicts with different candidate content",
                    artifact_id=artifact.artifact_id,
                )
            cur.execute(
                """
                UPDATE assumption_artifacts
                SET last_verified_at = GREATEST(last_verified_at, %s),
                    updated_at = NOW()
                WHERE variable_key = %s
                  AND effective_year = %s
                  AND artifact_status = 'approved'
                  AND governance_status = 'active'
                  AND content_fingerprint = %s
                """,
                (
                    batch.retrieved_at,
                    artifact.variable_key,
                    batch.effective_year,
                    fingerprint,
                ),
            )

    def get(self, artifact_id: str) -> AssumptionArtifact | None:
        pool = self._pool_or_none()
        if pool is None:
            return self.fallback.get(artifact_id)
        conn = None
        try:
            conn = core._safe_getconn(pool)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT artifact, last_verified_at
                    FROM assumption_artifacts
                    WHERE artifact_id = %s
                    """,
                    (artifact_id,),
                )
                row = cur.fetchone()
            return (
                _hydrate_artifact(row[0], last_verified_at=row[1])
                if row
                else None
            )
        except Exception as exc:
            self._handle_failure("artifact read", exc)
            return self.fallback.get(artifact_id)
        finally:
            if conn is not None:
                pool.putconn(conn)

    def latest(
        self,
        variable_key: str,
        *,
        effective_year: int | None = None,
    ) -> AssumptionArtifact | None:
        pool = self._pool_or_none()
        if pool is None:
            return self.fallback.latest(
                variable_key,
                effective_year=effective_year,
            )
        where_year = "AND effective_year = %s" if effective_year is not None else ""
        params: tuple[Any, ...] = (
            (variable_key, effective_year)
            if effective_year is not None
            else (variable_key,)
        )
        conn = None
        try:
            conn = core._safe_getconn(pool)
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT artifact, governance_status, last_verified_at
                    FROM assumption_artifacts
                    WHERE variable_key = %s
                      {where_year}
                      AND NOT (
                          artifact_status = 'candidate'
                          AND governance_status = 'approved'
                      )
                    ORDER BY
                      CASE
                        WHEN artifact_status = 'approved'
                             AND governance_status = 'active' THEN 5
                        WHEN artifact_status = 'candidate'
                             AND governance_status = 'pending' THEN 4
                        WHEN governance_status = 'rejected' THEN 2
                        WHEN governance_status = 'superseded' THEN 1
                        ELSE 0
                      END DESC,
                      approved_version DESC NULLS LAST,
                      created_at DESC
                    LIMIT 1
                    """,
                    params,
                )
                row = cur.fetchone()
            return (
                _hydrate_artifact(
                    row[0],
                    governance_status=row[1],
                    last_verified_at=row[2],
                )
                if row
                else None
            )
        except Exception as exc:
            self._handle_failure("latest read", exc)
            return self.fallback.latest(
                variable_key,
                effective_year=effective_year,
            )
        finally:
            if conn is not None:
                pool.putconn(conn)

    def latest_approved(
        self,
        variable_key: str,
        *,
        effective_year: int,
    ) -> AssumptionArtifact | None:
        pool = self._pool_or_none()
        if pool is None:
            return self.fallback.latest_approved(
                variable_key,
                effective_year=effective_year,
            )
        conn = None
        try:
            conn = core._safe_getconn(pool)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT artifact, last_verified_at
                    FROM assumption_artifacts
                    WHERE variable_key = %s
                      AND effective_year = %s
                      AND artifact_status = 'approved'
                      AND governance_status = 'active'
                    ORDER BY approved_version DESC, created_at DESC
                    LIMIT 1
                    """,
                    (variable_key, effective_year),
                )
                row = cur.fetchone()
            return (
                _hydrate_artifact(row[0], last_verified_at=row[1])
                if row
                else None
            )
        except Exception as exc:
            self._handle_failure("approved read", exc)
            return self.fallback.latest_approved(
                variable_key,
                effective_year=effective_year,
            )
        finally:
            if conn is not None:
                pool.putconn(conn)

    def durable_promotion_available(self) -> bool:
        """Auto-promotion requires the shared durable database, never fallback."""

        return self._pool_or_none() is not None

    def apply_decision(
        self,
        *,
        request: AssumptionReviewRequest,
        reviewer_id: str,
        decided_at: datetime,
        policy_id: str,
        policy_version: int,
        approved_uses: tuple[PermittedUse, ...],
        promotion_examination: DurableFactPromotionExamination | None = None,
    ) -> AssumptionDecisionRecord:
        return self._apply_decision(
            request=request,
            reviewer_id=reviewer_id,
            decided_at=decided_at,
            policy_id=policy_id,
            policy_version=policy_version,
            approved_uses=approved_uses,
            promotion_examination=promotion_examination,
            promotion_batch=None,
        )

    def apply_verified_promotion(
        self,
        *,
        batch: ProviderCandidateBatch,
        request: AssumptionReviewRequest,
        reviewer_id: str,
        decided_at: datetime,
        policy_id: str,
        policy_version: int,
        approved_uses: tuple[PermittedUse, ...],
        promotion_examination: DurableFactPromotionExamination,
    ) -> AssumptionDecisionRecord:
        """Atomically insert and activate one independently verified fact."""

        return self._apply_decision(
            request=request,
            reviewer_id=reviewer_id,
            decided_at=decided_at,
            policy_id=policy_id,
            policy_version=policy_version,
            approved_uses=approved_uses,
            promotion_examination=promotion_examination,
            promotion_batch=batch,
        )

    def _apply_decision(
        self,
        *,
        request: AssumptionReviewRequest,
        reviewer_id: str,
        decided_at: datetime,
        policy_id: str,
        policy_version: int,
        approved_uses: tuple[PermittedUse, ...],
        promotion_examination: DurableFactPromotionExamination | None,
        promotion_batch: ProviderCandidateBatch | None,
    ) -> AssumptionDecisionRecord:
        if (promotion_batch is None) != (promotion_examination is None):
            raise AssumptionGovernanceError(
                GovernanceErrorCode.DECISION_CONFLICT,
                "verified promotion requires one matching batch and examination",
                artifact_id=request.candidate_artifact_id,
            )
        if promotion_batch is not None:
            verification = promotion_examination.verification
            if (
                len(promotion_batch.artifacts) != 1
                or promotion_batch.artifacts[0].artifact_id
                != request.candidate_artifact_id
                or verification is None
                or verification.provider_id != promotion_batch.provider_id
                or verification.source_snapshot_sha256
                != promotion_batch.snapshot_sha256
            ):
                raise AssumptionGovernanceError(
                    GovernanceErrorCode.DECISION_CONFLICT,
                    "promotion batch does not match the examination",
                    artifact_id=request.candidate_artifact_id,
                )
        pool = self._pool_or_none()
        if pool is None:
            if promotion_examination is not None:
                raise AssumptionGovernanceError(
                    GovernanceErrorCode.PERSISTENCE_UNAVAILABLE,
                    "durable assumption promotion storage is unavailable",
                    artifact_id=request.candidate_artifact_id,
                )
            return self.fallback.apply_decision(
                request=request,
                reviewer_id=reviewer_id,
                decided_at=decided_at,
                policy_id=policy_id,
                policy_version=policy_version,
                approved_uses=approved_uses,
                promotion_examination=None,
            )
        conn = None
        try:
            conn = core._safe_getconn(pool)
            with conn.cursor() as cur:
                prior = self._idempotent_decision(
                    cur,
                    request,
                    reviewer_id=reviewer_id,
                    promotion_examination=promotion_examination,
                )
                if prior is not None:
                    conn.commit()
                    return prior

                if promotion_batch is not None:
                    self._insert_candidate_batch(cur, promotion_batch)

                cur.execute(
                    """
                    SELECT artifact, content_fingerprint, governance_status,
                           provider_id, effective_year,
                           source_snapshot_sha256, last_verified_at
                    FROM assumption_artifacts
                    WHERE artifact_id = %s
                    FOR UPDATE
                    """,
                    (request.candidate_artifact_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise AssumptionGovernanceError(
                        GovernanceErrorCode.CANDIDATE_NOT_FOUND,
                        "assumption candidate was not found",
                        artifact_id=request.candidate_artifact_id,
                    )
                (
                    raw_candidate,
                    stored_fingerprint,
                    governance_status,
                    provider_id,
                    effective_year,
                    snapshot_sha256,
                    last_verified_at,
                ) = row
                candidate = _hydrate_artifact(
                    raw_candidate,
                    last_verified_at=last_verified_at,
                )
                if (
                    stored_fingerprint != request.expected_fingerprint
                    or assumption_artifact_fingerprint(candidate)
                    != request.expected_fingerprint
                ):
                    raise AssumptionGovernanceError(
                        GovernanceErrorCode.FINGERPRINT_MISMATCH,
                        "candidate fingerprint changed before persistence",
                        artifact_id=candidate.artifact_id,
                    )
                if promotion_examination is not None:
                    examined_candidate = promotion_examination.candidate
                    if (
                        request.decision is not GovernanceDecision.APPROVE
                        or examined_candidate.artifact_id != candidate.artifact_id
                        or examined_candidate.artifact_fingerprint
                        != request.expected_fingerprint
                        or promotion_examination.policy is None
                        or promotion_examination.policy.policy_id != policy_id
                        or promotion_examination.policy.policy_version
                        != policy_version
                        or tuple(promotion_examination.policy.granted_uses)
                        != tuple(approved_uses)
                        or promotion_examination.verification is None
                        or promotion_examination.verification.source_snapshot_sha256
                        != str(snapshot_sha256 or "")
                    ):
                        raise AssumptionGovernanceError(
                            GovernanceErrorCode.DECISION_CONFLICT,
                            "promotion examination does not match the candidate decision",
                            artifact_id=candidate.artifact_id,
                        )
                if governance_status != "pending":
                    raise AssumptionGovernanceError(
                        GovernanceErrorCode.DECISION_CONFLICT,
                        "candidate already has a final decision",
                        artifact_id=candidate.artifact_id,
                    )
                cur.execute(
                    """
                    SELECT decision_record
                    FROM assumption_decisions
                    WHERE candidate_artifact_id = %s
                    """,
                    (candidate.artifact_id,),
                )
                if cur.fetchone() is not None:
                    raise AssumptionGovernanceError(
                        GovernanceErrorCode.DECISION_CONFLICT,
                        "candidate already has a final decision",
                        artifact_id=candidate.artifact_id,
                    )

                approved: AssumptionArtifact | None = None
                supersedes_artifact_id: str | None = None
                if request.decision is GovernanceDecision.APPROVE:
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"{candidate.variable_key}:{effective_year}",),
                    )
                    cur.execute(
                        """
                        SELECT artifact_id, approved_version,
                               content_fingerprint
                        FROM assumption_artifacts
                        WHERE variable_key = %s
                          AND effective_year = %s
                          AND artifact_status = 'approved'
                          AND governance_status = 'active'
                        ORDER BY approved_version DESC
                        LIMIT 1
                        FOR UPDATE
                        """,
                        (candidate.variable_key, effective_year),
                    )
                    current = cur.fetchone()
                    if promotion_examination is not None:
                        expected = promotion_examination.expected_active
                        if (
                            (str(current[0]) if current else None)
                            != expected.artifact_id
                            or (int(current[1]) if current else None)
                            != expected.artifact_version
                            or (str(current[2]) if current else None)
                            != expected.artifact_fingerprint
                        ):
                            raise AssumptionGovernanceError(
                                GovernanceErrorCode.DECISION_CONFLICT,
                                "active assumption changed after promotion examination",
                                artifact_id=candidate.artifact_id,
                            )
                    supersedes_artifact_id = str(current[0]) if current else None
                    cur.execute(
                        """
                        SELECT COALESCE(MAX(approved_version), 0)
                        FROM assumption_artifacts
                        WHERE variable_key = %s AND effective_year = %s
                        """,
                        (candidate.variable_key, effective_year),
                    )
                    approved_version = int(cur.fetchone()[0]) + 1
                    approved = build_approved_assumption(
                        candidate=candidate,
                        reviewer_id=reviewer_id,
                        decided_at=decided_at,
                        approved_version=approved_version,
                        approved_uses=approved_uses,
                    )
                    if promotion_examination is not None and (
                        promotion_examination.decision.approved_artifact_id
                        != approved.artifact_id
                        or promotion_examination.decision.approved_version
                        != approved.assumption_set_version
                        or promotion_examination.decision.supersedes_artifact_id
                        != supersedes_artifact_id
                    ):
                        raise AssumptionGovernanceError(
                            GovernanceErrorCode.DECISION_CONFLICT,
                            "promotion examination predicted different durable state",
                            artifact_id=candidate.artifact_id,
                        )
                    if supersedes_artifact_id:
                        cur.execute(
                            """
                            UPDATE assumption_artifacts
                            SET governance_status = 'superseded',
                                updated_at = NOW()
                            WHERE artifact_id = %s
                            """,
                            (supersedes_artifact_id,),
                        )
                    cur.execute(
                        """
                        INSERT INTO assumption_artifacts
                            (artifact_id, variable_key, provider_id,
                             effective_year, source_class, artifact_status,
                             governance_status, content_fingerprint,
                             source_snapshot_sha256, assumption_set_id,
                             approved_version, artifact, created_at,
                             last_verified_at)
                        VALUES
                            (%s, %s, %s, %s, %s, 'approved', 'active',
                             %s, %s, %s, %s, %s::jsonb, %s, %s)
                        """,
                        (
                            approved.artifact_id,
                            approved.variable_key,
                            provider_id,
                            effective_year,
                            approved.source_class.value,
                            request.expected_fingerprint,
                            snapshot_sha256,
                            approved.assumption_set_id,
                            approved.assumption_set_version,
                            core._pg_json(approved.model_dump(mode="json")),
                            approved.created_at,
                            last_verified_at,
                        ),
                    )
                    candidate_governance_status = "approved"
                else:
                    candidate_governance_status = "rejected"

                cur.execute(
                    """
                    UPDATE assumption_artifacts
                    SET governance_status = %s, updated_at = NOW()
                    WHERE artifact_id = %s
                    """,
                    (candidate_governance_status, candidate.artifact_id),
                )
                decision = build_assumption_decision_record(
                    request=request,
                    candidate=candidate,
                    reviewer_id=reviewer_id,
                    decided_at=decided_at,
                    policy_id=policy_id,
                    policy_version=policy_version,
                    approved=approved,
                    supersedes_artifact_id=supersedes_artifact_id,
                    promotion_examination=promotion_examination,
                )
                cur.execute(
                    """
                    INSERT INTO assumption_decisions
                        (decision_id, candidate_artifact_id,
                         candidate_fingerprint, variable_key, effective_year,
                         decision, decided_by, decided_at, reason,
                         idempotency_key, approved_artifact_id,
                         approved_version, supersedes_artifact_id,
                         policy_id, policy_version, decision_record)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                         %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        decision.decision_id,
                        decision.candidate_artifact_id,
                        decision.candidate_fingerprint,
                        decision.variable_key,
                        decision.effective_year,
                        decision.decision.value,
                        decision.decided_by,
                        decision.decided_at,
                        decision.reason,
                        decision.idempotency_key,
                        decision.approved_artifact_id,
                        decision.approved_version,
                        decision.supersedes_artifact_id,
                        decision.policy_id,
                        decision.policy_version,
                        core._pg_json(decision.model_dump(mode="json")),
                    ),
                )
            conn.commit()
            return decision
        except Exception as exc:
            if conn is not None:
                conn.rollback()
            if isinstance(exc, AssumptionGovernanceError):
                raise
            if promotion_examination is not None:
                raise AssumptionGovernanceError(
                    GovernanceErrorCode.PERSISTENCE_UNAVAILABLE,
                    "durable assumption promotion failed",
                    artifact_id=request.candidate_artifact_id,
                ) from exc
            self._handle_failure("decision write", exc)
            return self.fallback.apply_decision(
                request=request,
                reviewer_id=reviewer_id,
                decided_at=decided_at,
                policy_id=policy_id,
                policy_version=policy_version,
                approved_uses=approved_uses,
                promotion_examination=None,
            )
        finally:
            if conn is not None:
                pool.putconn(conn)

    def decision_history(
        self,
        candidate_artifact_id: str,
    ) -> tuple[AssumptionDecisionRecord, ...]:
        pool = self._pool_or_none()
        if pool is None:
            return self.fallback.decision_history(candidate_artifact_id)
        conn = None
        try:
            conn = core._safe_getconn(pool)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT decision_record
                    FROM assumption_decisions
                    WHERE candidate_artifact_id = %s
                    ORDER BY decided_at, decision_id
                    """,
                    (candidate_artifact_id,),
                )
                rows = cur.fetchall()
            return tuple(
                AssumptionDecisionRecord.model_validate(_json_value(row[0]))
                for row in rows
            )
        except Exception as exc:
            self._handle_failure("decision history read", exc)
            return self.fallback.decision_history(candidate_artifact_id)
        finally:
            if conn is not None:
                pool.putconn(conn)

    def list_candidates(
        self,
        *,
        variable_key: str | None = None,
        effective_year: int | None = None,
        governance_status: str | None = None,
        limit: int = 100,
    ) -> tuple[AssumptionCandidateReview, ...]:
        if governance_status not in {None, "pending", "approved", "rejected"}:
            raise ValueError("unsupported governance_status")
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        pool = self._pool_or_none()
        if pool is None:
            return self.fallback.list_candidates(
                variable_key=variable_key,
                effective_year=effective_year,
                governance_status=governance_status,
                limit=limit,
            )

        filters = ["a.artifact_status = 'candidate'"]
        params: list[Any] = []
        if variable_key:
            filters.append("a.variable_key = %s")
            params.append(variable_key)
        if effective_year is not None:
            filters.append("a.effective_year = %s")
            params.append(effective_year)
        if governance_status:
            filters.append("a.governance_status = %s")
            params.append(governance_status)
        params.append(limit)

        conn = None
        try:
            conn = core._safe_getconn(pool)
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT a.artifact, a.content_fingerprint,
                           a.governance_status, a.last_verified_at,
                           d.decision_record
                    FROM assumption_artifacts AS a
                    LEFT JOIN LATERAL (
                        SELECT decision_record
                        FROM assumption_decisions
                        WHERE candidate_artifact_id = a.artifact_id
                        ORDER BY decided_at DESC, decision_id DESC
                        LIMIT 1
                    ) AS d ON TRUE
                    WHERE {" AND ".join(filters)}
                    ORDER BY a.created_at DESC, a.artifact_id DESC
                    LIMIT %s
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()
            return tuple(
                AssumptionCandidateReview(
                    candidate=_hydrate_artifact(
                        row[0],
                        last_verified_at=row[3],
                    ),
                    fingerprint=str(row[1]),
                    governance_status=str(row[2]),
                    decision=(
                        AssumptionDecisionRecord.model_validate(
                            _json_value(row[4])
                        )
                        if row[4] is not None
                        else None
                    ),
                )
                for row in rows
            )
        except Exception as exc:
            self._handle_failure("candidate list", exc)
            return self.fallback.list_candidates(
                variable_key=variable_key,
                effective_year=effective_year,
                governance_status=governance_status,
                limit=limit,
            )
        finally:
            if conn is not None:
                pool.putconn(conn)

    @staticmethod
    def _idempotent_decision(
        cur,
        request: AssumptionReviewRequest,
        *,
        reviewer_id: str,
        promotion_examination: DurableFactPromotionExamination | None = None,
    ) -> AssumptionDecisionRecord | None:
        cur.execute(
            """
            SELECT decision_record
            FROM assumption_decisions
            WHERE idempotency_key = %s
            """,
            (request.idempotency_key,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        decision = AssumptionDecisionRecord.model_validate(_json_value(row[0]))
        if (
            decision.candidate_artifact_id == request.candidate_artifact_id
            and decision.candidate_fingerprint == request.expected_fingerprint
            and decision.decision is request.decision
            and decision.reason == request.reason
            and decision.decided_by == reviewer_id
            and (
                (
                    decision.promotion_examination is None
                    and promotion_examination is None
                )
                or (
                    decision.promotion_examination is not None
                    and promotion_examination is not None
                    and decision.promotion_examination.examination_id
                    == promotion_examination.examination_id
                )
            )
        ):
            return decision
        raise AssumptionGovernanceError(
            GovernanceErrorCode.DECISION_CONFLICT,
            "idempotency key was already used for another decision",
            artifact_id=request.candidate_artifact_id,
        )


def build_assumption_repository() -> PostgresAssumptionRepository:
    """Build the PostgreSQL-first repository with process-local fallback."""

    return PostgresAssumptionRepository()


_ASSUMPTION_REPOSITORY_LOCK = RLock()
_ASSUMPTION_REPOSITORY: PostgresAssumptionRepository | None = None


def get_assumption_repository() -> PostgresAssumptionRepository:
    """Return the process singleton used by adapters, review APIs, and shadow."""

    global _ASSUMPTION_REPOSITORY
    with _ASSUMPTION_REPOSITORY_LOCK:
        if _ASSUMPTION_REPOSITORY is None:
            _ASSUMPTION_REPOSITORY = build_assumption_repository()
        return _ASSUMPTION_REPOSITORY

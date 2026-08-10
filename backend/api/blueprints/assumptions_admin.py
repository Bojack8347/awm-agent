"""Authenticated human-review routes for governed assumption candidates."""

from __future__ import annotations

from typing import Any, Callable, Tuple

from flask import Blueprint, jsonify, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from advisor.assumptions.contracts import AssumptionStatus
from advisor.assumptions.governance import (
    AssumptionApprovalService,
    AssumptionGovernanceError,
    AssumptionReviewRequest,
    GovernanceDecision,
    GovernanceErrorCode,
    assumption_artifact_fingerprint,
)


class _DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: GovernanceDecision
    reason: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=240)


def create_assumptions_admin_blueprint(
    *,
    api_key_auth_decorator: Callable[[Any], Any],
    repository_factory: Callable[[], Any],
    reviewer_identity_factory: Callable[[], str],
) -> Blueprint:
    """Create a fail-closed API-key admin surface.

    The review body contains no reviewer field. Production supplies reviewer
    identity from server configuration associated with the authenticated admin
    API boundary.
    """

    bp = Blueprint("assumptions_admin", __name__)

    def _reviewer_id() -> str:
        return str(reviewer_identity_factory() or "").strip()

    def _not_configured() -> Tuple[Any, int]:
        return jsonify(
            {
                "success": False,
                "error": "Assumption admin authentication is not configured",
                "code": "admin_auth_not_configured",
            }
        ), 503

    @bp.route("/api/v1/admin/assumptions/candidates", methods=["GET"])
    @api_key_auth_decorator
    def list_candidates() -> Tuple[Any, int]:
        if not _reviewer_id():
            return _not_configured()
        try:
            effective_year = _optional_int_query("effective_year")
            limit = _bounded_limit()
            governance_status = _optional_status_query()
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

        reviews = repository_factory().list_candidates(
            variable_key=_optional_text_query("variable_key"),
            effective_year=effective_year,
            governance_status=governance_status,
            limit=limit,
        )
        return jsonify(
            {
                "success": True,
                "candidates": [
                    review.model_dump(mode="json") for review in reviews
                ],
            }
        ), 200

    @bp.route(
        "/api/v1/admin/assumptions/<artifact_id>/decision",
        methods=["POST"],
    )
    @api_key_auth_decorator
    def decide_candidate(artifact_id: str) -> Tuple[Any, int]:
        reviewer_id = _reviewer_id()
        if not reviewer_id:
            return _not_configured()
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not body:
            return jsonify(
                {"success": False, "error": "Request JSON body is required"}
            ), 400
        try:
            decision_body = _DecisionBody.model_validate(body)
            review_request = AssumptionReviewRequest(
                candidate_artifact_id=artifact_id,
                **decision_body.model_dump(),
            )
            decision = AssumptionApprovalService(
                repository=repository_factory()
            ).review(
                review_request,
                reviewer_id=reviewer_id,
            )
        except ValidationError as exc:
            return jsonify(
                {
                    "success": False,
                    "error": "Invalid assumption decision",
                    "details": exc.errors(
                        include_url=False,
                        include_context=False,
                    ),
                }
            ), 400
        except AssumptionGovernanceError as exc:
            return _governance_error_response(exc)

        return jsonify(
            {
                "success": True,
                "decision": decision.model_dump(mode="json"),
            }
        ), 200

    @bp.route(
        "/api/v1/admin/assumptions/<artifact_id>/history",
        methods=["GET"],
    )
    @api_key_auth_decorator
    def candidate_history(artifact_id: str) -> Tuple[Any, int]:
        if not _reviewer_id():
            return _not_configured()
        repository = repository_factory()
        candidate = repository.get(artifact_id)
        if candidate is None or candidate.status is not AssumptionStatus.CANDIDATE:
            return jsonify(
                {
                    "success": False,
                    "error": "Assumption candidate was not found",
                    "code": GovernanceErrorCode.CANDIDATE_NOT_FOUND.value,
                }
            ), 404
        history = repository.decision_history(artifact_id)
        return jsonify(
            {
                "success": True,
                "candidate": candidate.model_dump(mode="json"),
                "fingerprint": assumption_artifact_fingerprint(candidate),
                "history": [
                    decision.model_dump(mode="json") for decision in history
                ],
            }
        ), 200

    return bp


def _optional_text_query(name: str) -> str | None:
    value = str(request.args.get(name) or "").strip()
    return value or None


def _optional_int_query(name: str) -> int | None:
    value = _optional_text_query(name)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < 2000 or parsed > 2200:
        raise ValueError(f"{name} must be between 2000 and 2200")
    return parsed


def _bounded_limit() -> int:
    value = _optional_text_query("limit")
    if value is None:
        return 100
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError("limit must be an integer") from exc
    if parsed < 1 or parsed > 500:
        raise ValueError("limit must be between 1 and 500")
    return parsed


def _optional_status_query() -> str | None:
    status = _optional_text_query("status")
    if status not in {None, "pending", "approved", "rejected"}:
        raise ValueError("status must be pending, approved, or rejected")
    return status


def _governance_error_response(
    exc: AssumptionGovernanceError,
) -> Tuple[Any, int]:
    status_by_code = {
        GovernanceErrorCode.CANDIDATE_NOT_FOUND: 404,
        GovernanceErrorCode.FINGERPRINT_MISMATCH: 409,
        GovernanceErrorCode.DECISION_CONFLICT: 409,
        GovernanceErrorCode.PERSISTENCE_UNAVAILABLE: 503,
        GovernanceErrorCode.REVIEWER_REQUIRED: 503,
    }
    return jsonify(
        {
            "success": False,
            "error": str(exc),
            "code": exc.code.value,
            "artifact_id": exc.artifact_id,
        }
    ), status_by_code.get(exc.code, 422)


__all__ = ["create_assumptions_admin_blueprint"]

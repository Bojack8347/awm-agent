"""Persistent v2 tool handlers that write to the Client File stores.

These handlers belong to the v2 tool boundary. They may reuse low-level
repository functions, but they must not call legacy companion orchestration,
agent prompts, or runtime dispatchers.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional


DbCallable = Callable[..., Any]


def _money_pool_error_code(exc: Exception) -> str:
    """Translate durable repository failures into the public tool contract."""

    return {
        "DatabaseModeUnsupportedError": "database_mode_unsupported",
        "DatabaseUnavailableError": "database_unavailable",
        "ClientIdentityMissingError": "client_identity_missing",
        "PersistenceValidationError": "persistence_validation_failed",
        "SchemaOutdatedError": "schema_outdated",
        "UnknownPersistenceError": "unknown_persistence_failure",
    }.get(exc.__class__.__name__, "unknown_persistence_failure")


class V2PersistentToolHandlers:
    """Deterministic persistence adapter for v2 write tools."""

    def __init__(
        self,
        *,
        db_upsert_money_pool: Optional[DbCallable] = None,
        db_list_artifacts: Optional[DbCallable] = None,
        db_update_artifact: Optional[DbCallable] = None,
        db_list_policies: Optional[DbCallable] = None,
        db_update_policy: Optional[DbCallable] = None,
    ) -> None:
        self._db_upsert_money_pool = db_upsert_money_pool
        self._db_list_artifacts = db_list_artifacts
        self._db_update_artifact = db_update_artifact
        self._db_list_policies = db_list_policies
        self._db_update_policy = db_update_policy

    def execute(
        self,
        *,
        client_id: str,
        session_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if tool_name == "upsert_money_pool":
            return self.upsert_money_pool(client_id=client_id, arguments=arguments)
        if tool_name == "record_policy_review_outcome":
            return self.record_policy_review_outcome(
                client_id=client_id,
                session_id=session_id,
                arguments=arguments,
            )
        return {"tool": tool_name, "ok": False, "error": "unknown_persistent_tool"}

    def upsert_money_pool(self, *, client_id: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        label = str(arguments.get("label") or "").strip()
        if not label:
            return {"tool": "upsert_money_pool", "ok": False, "error": "label_required"}

        try:
            pool = self._upsert_money_pool()(client_id, dict(arguments))
        except Exception as exc:  # Repository errors are translated at this boundary.
            error = _money_pool_error_code(exc)
            return {
                "tool": "upsert_money_pool",
                "ok": False,
                "error": error,
                # Even a transient outage is not retried conversationally; the
                # client should not be put into a futile retry-solicitation loop.
                "retryable": False,
            }
        if pool is None:
            return {"tool": "upsert_money_pool", "ok": False, "error": "save_failed"}
        return {
            "tool": "upsert_money_pool",
            "ok": True,
            "adapter": "v2_persistent_tool_handlers",
            "label": pool.get("label") or label,
            "pool_id": pool.get("id"),
            "amount": pool.get("amount"),
            "state": pool.get("state"),
            "payload": pool,
        }

    def record_policy_review_outcome(
        self,
        *,
        client_id: str,
        session_id: str,
        arguments: Mapping[str, Any],
    ) -> Dict[str, Any]:
        target_type = str(arguments.get("target_type") or "").strip().lower()
        decision = str(arguments.get("decision") or "").strip().lower()
        target_id = str(arguments.get("target_id") or "").strip()
        user_message = _source_user_message(arguments)

        if target_type not in {"proposal", "policy"}:
            return {"tool": "record_policy_review_outcome", "ok": False, "error": "target_type_required"}
        if decision not in {"approve", "refine", "defer", "keep_unchanged"}:
            return {"tool": "record_policy_review_outcome", "ok": False, "error": "decision_required"}
        if not _has_explicit_review_decision(decision, user_message):
            return {"tool": "record_policy_review_outcome", "ok": False, "error": "ambiguous_review_decision"}
        if target_type == "proposal" and decision == "keep_unchanged":
            return {
                "tool": "record_policy_review_outcome",
                "ok": False,
                "error": "decision_not_valid_for_proposal",
            }
        if target_type == "policy" and decision in {"approve", "refine", "defer"}:
            target_type = "proposal"

        if target_type == "proposal":
            rows = self._list_artifacts()(client_id=client_id, artifact_type="proposal") or []
            target = _select_review_target(rows, target_id=target_id, target_type=target_type)
            if not target:
                return {"tool": "record_policy_review_outcome", "ok": False, "error": "proposal_not_found"}
            new_status = {
                "approve": "approved",
                "refine": "needs_revision",
                "defer": "deferred",
            }[decision]
            patch = _review_outcome_patch(
                target_type=target_type,
                decision=decision,
                rationale=arguments.get("rationale"),
                session_id=session_id,
                user_message=user_message,
                previous_stale_review=(target.get("payload") or {}).get("stale_review"),
            )
            updated = self._update_artifact()(
                artifact_id=str(target.get("id")),
                client_id=client_id,
                status=new_status,
                payload_patch=patch,
            )
        else:
            rows = self._list_policies()(client_id=client_id, status=None) or []
            target = _select_review_target(rows, target_id=target_id, target_type=target_type)
            if not target:
                return {"tool": "record_policy_review_outcome", "ok": False, "error": "policy_not_found"}
            current_status = str(target.get("status") or "active")
            review_status = "resolved" if decision == "keep_unchanged" else "needs_revision"
            patch = _review_outcome_patch(
                target_type=target_type,
                decision=decision,
                rationale=arguments.get("rationale"),
                session_id=session_id,
                user_message=user_message,
                stale_review_status=review_status,
                previous_stale_review=(target.get("payload") or {}).get("stale_review"),
            )
            updated = self._update_policy()(
                policy_id=str(target.get("id")),
                client_id=client_id,
                status=current_status,
                payload_patch=patch,
            )

        if not updated:
            return {"tool": "record_policy_review_outcome", "ok": False, "error": "save_failed"}
        return {
            "tool": "record_policy_review_outcome",
            "ok": True,
            "adapter": "v2_persistent_tool_handlers",
            "target_type": target_type,
            "target_id": updated.get("id"),
            "decision": decision,
            "status": updated.get("status"),
            "review_status": (((updated.get("payload") or {}).get("stale_review") or {}).get("status")),
        }

    def _upsert_money_pool(self) -> DbCallable:
        if self._db_upsert_money_pool:
            return self._db_upsert_money_pool
        from api.persistence import upsert_money_pool

        return upsert_money_pool

    def _list_artifacts(self) -> DbCallable:
        if self._db_list_artifacts:
            return self._db_list_artifacts
        from api.persistence import list_artifacts

        return list_artifacts

    def _update_artifact(self) -> DbCallable:
        if self._db_update_artifact:
            return self._db_update_artifact
        from api.persistence import update_artifact

        return update_artifact

    def _list_policies(self) -> DbCallable:
        if self._db_list_policies:
            return self._db_list_policies
        from api.persistence import list_policies

        return list_policies

    def _update_policy(self) -> DbCallable:
        if self._db_update_policy:
            return self._db_update_policy
        from api.persistence import update_policy

        return update_policy


def _source_user_message(arguments: Mapping[str, Any]) -> str:
    for key in ("user_message", "question", "rationale"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _select_review_target(
    rows: Any,
    *,
    target_id: str,
    target_type: str,
) -> Optional[Dict[str, Any]]:
    clean = [row for row in rows if isinstance(row, dict)]
    if target_id:
        exact = next((row for row in clean if str(row.get("id") or "") == target_id), None)
        if exact:
            return exact
    if target_type == "proposal":
        preferred = {"pending_review", "ready", "proposed"}
        return next((row for row in clean if str(row.get("status") or "").lower() in preferred), None)
    return next(
        (
            row
            for row in clean
            if (((row.get("payload") or {}).get("stale_review") or {}).get("status") == "needs_review")
        ),
        clean[0] if clean else None,
    )


def _review_outcome_patch(
    *,
    target_type: str,
    decision: str,
    rationale: Any,
    session_id: str,
    user_message: str,
    stale_review_status: Optional[str] = None,
    previous_stale_review: Any = None,
) -> Dict[str, Any]:
    prior_stale = previous_stale_review if isinstance(previous_stale_review, dict) else {}
    outcome = {
        "decision": decision,
        "rationale": str(rationale or user_message or "")[:500],
        "source": {
            "engagement_type": "companion_turn",
            "session_id": session_id,
            "user_message": str(user_message or "")[:500],
            "source_id": prior_stale.get("source_id"),
            "source_subject": prior_stale.get("source_subject"),
        },
    }
    patch: Dict[str, Any] = {"review_outcome": outcome}
    if target_type == "proposal" and prior_stale:
        patch["stale_review"] = {
            **prior_stale,
            "status": "resolved",
            "resolved_by": decision,
            "rationale": outcome["rationale"],
            "source": outcome["source"],
        }
    if target_type == "policy":
        patch["stale_review"] = {
            "status": stale_review_status or "resolved",
            "resolved_by": decision,
            "rationale": outcome["rationale"],
            "source_id": prior_stale.get("source_id"),
            "source_subject": prior_stale.get("source_subject"),
            "source_event_id": prior_stale.get("source_event_id"),
            "source": outcome["source"],
        }
    return patch


def _has_explicit_review_decision(decision: str, user_message: str) -> bool:
    text = str(user_message or "").lower()
    evidence = {
        "approve": (
            "approve",
            "approved",
            "go ahead",
            "go with that",
            "go with it",
            "looks good",
            "looks right",
            "sounds fine",
            "sounds right",
            "accept",
            "accepted",
            "set it up",
        ),
        "refine": (
            "refine",
            "revise",
            "adjust",
            "change",
            "changes",
            "too risky",
            "too conservative",
            "less volatile",
            "less bumpy",
            "smoother",
            "smooth it out",
            "not right",
            "doesn't fit",
            "does not fit",
        ),
        "defer": (
            "defer",
            "deferred",
            "not now",
            "later",
            "wait",
            "hold off",
            "pause",
            "sit with it",
        ),
        "keep_unchanged": (
            "keep unchanged",
            "keep it unchanged",
            "keep the active policy unchanged",
            "leave unchanged",
            "leave it as is",
            "keep as is",
            "stay as is",
            "no change",
            "unchanged",
        ),
    }
    return any(phrase in text for phrase in evidence.get(decision, ()))

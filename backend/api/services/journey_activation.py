"""Journey policy activation workflow.

The journeys blueprint owns HTTP concerns. This service owns the activation
pipeline call, checkpoint resume handling, truth-version resolution, and state
transition for an already completed journey policy.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from contracts.journey import validate_journey_activation_response
from advisor.tasks.activation_mutation import ValidationError


def activate_journey_policy(
    deps: Any,
    *,
    journey_id: str,
    auth_session: Dict[str, Any],
) -> Tuple[Dict[str, Any], int]:
    """Activate a completed journey policy and return an HTTP payload."""
    journey = deps.db_get_journey_run(journey_id)
    if not journey:
        return _validated_response({"success": False, "error": "Journey not found"}, 404)
    if journey["client_id"] != auth_session["client_id"]:
        return _validated_response({"success": False, "error": "Forbidden"}, 403)

    if journey.get("state") == "activated":
        return _validated_response({
            "success": True,
            "journey_id": journey_id,
            "policy_status": "activated",
            "activated_at": journey.get("activated_at"),
            "activation_snapshot_version": journey.get("activation_snapshot_version"),
        }, 200)

    if journey.get("state") != "completed":
        return _validated_response({
            "success": False,
            "error": "Cannot activate a policy that has not completed solution generation",
        }, 400)

    client_id = journey["client_id"]
    solution = journey.get("solution_output", {})

    try:
        current_facts = deps.db_get_knowledge_facts(client_id)
        checkpoint = deps.db_load_checkpoint(journey_id, "activation")
        resume_after = None
        initial_context = {
            "journey_id": journey_id,
            "journey_type": journey["journey_type"],
            "client_id": client_id,
            "solution_output": solution,
            "current_facts": current_facts,
        }
        if checkpoint:
            resume_after = checkpoint["step_name"]
            initial_context.update(checkpoint["context"])

        result = deps.activation_pipeline().run(
            context=initial_context,
            run_id=journey_id,
            checkpoint_fn=deps.pipeline_checkpoint(),
            resume_after=resume_after,
        )
        deps.db_delete_checkpoint(journey_id, "activation")

        truth = result.context.get("truth_result")
        if isinstance(truth, dict):
            new_version = truth.get("snapshot_version") or deps.db_get_current_snapshot_version(client_id)
        else:
            new_version = truth.snapshot_version if truth else deps.db_get_current_snapshot_version(client_id)
        activated_at = datetime.now(timezone.utc).isoformat()

        from api.services.journey_lifecycle import transition_journey_state

        transition_journey_state(
            journey_id,
            new_state="activated",
            reason="user_activated_policy",
            extra_cols={
                "activated_at": activated_at,
                "activation_snapshot_version": new_version,
            },
        )
        _record_policy_activation_event(
            journey_id=journey_id,
            request_id=uuid.uuid4().hex[:12],
            activated_at=activated_at,
            activation_snapshot_version=new_version,
        )
        _mark_advisory_activation(
            journey_id=journey_id,
            activated_at=activated_at,
            activation_snapshot_version=new_version,
        )
        _trace_policy_activation(
            client_id=client_id,
            journey_id=journey_id,
            activated_at=activated_at,
            activation_snapshot_version=new_version,
        )

        return _validated_response({
            "success": True,
            "journey_id": journey_id,
            "policy_status": "activated",
            "activated_at": activated_at,
            "activation_snapshot_version": new_version,
            "diagnosis_refreshed": result.context.get("diagnosis_version") is not None,
            "diagnosis_status": result.context.get("diagnosis_status", "not_required"),
            "diagnosis_refresh_queued": bool(result.context.get("diagnosis_refresh_queued")),
        }, 200)

    except ValidationError as exc:
        return _validated_response({
            "success": False,
            "error": "Activation mutation validation failed",
            "details": exc.errors,
        }, 422)

    except Exception as exc:  # pylint: disable=broad-except
        print(f"[activation] Policy activation failed: {exc}", flush=True)
        return _validated_response({
            "success": False,
            "error": "Policy activation failed",
        }, 500)


def _validated_response(payload: Dict[str, Any], status: int) -> Tuple[Dict[str, Any], int]:
    return validate_journey_activation_response(payload), status


def _record_policy_activation_event(
    *,
    journey_id: str,
    request_id: Any,
    activated_at: str,
    activation_snapshot_version: Any,
) -> None:
    """Record the mobile activation action in the journey event trail."""
    try:
        from api.persistence import append_journey_event  # type: ignore

        append_journey_event(
            journey_id=journey_id,
            event_type="ui_selection",
            source="mobile_ui",
            actor="mobile_ui",
            payload={
                "element_id": "policy.activate",
                "value": {
                    "action": "activate_policy",
                    "activated_at": activated_at,
                    "activation_snapshot_version": activation_snapshot_version,
                },
            },
            applied_state="activated",
            idempotency_key=f"ui:activate_policy:{journey_id}",
            request_id=str(request_id or ""),
        )
    except Exception as exc:
        print(f"[activation] Failed to record policy activation event: {exc}", flush=True)


def _mark_advisory_activation(
    *,
    journey_id: str,
    activated_at: str,
    activation_snapshot_version: Any,
) -> None:
    """Keep the advisory mirror in sync with the legacy journey activation."""
    try:
        from api.persistence import mark_advisory_plan_status_for_journey  # type: ignore

        mark_advisory_plan_status_for_journey(
            journey_id=journey_id,
            status="accepted",
            decision="accepted",
            decided_at=activated_at,
            decision_payload={
                "source": "journey_activation",
                "activation_snapshot_version": activation_snapshot_version,
            },
        )
    except Exception as exc:
        print(f"[activation] Failed to mark advisory activation: {exc}", flush=True)


def _trace_policy_activation(
    *,
    client_id: str,
    journey_id: str,
    activated_at: str,
    activation_snapshot_version: Any,
) -> None:
    try:
        from api.persistence import create_trace_event, get_advisory_plan_for_journey  # type: ignore

        advisory = get_advisory_plan_for_journey(journey_id) or {}
        plan_id = advisory.get("id")
        create_trace_event(
            trace_id=f"tr_activate_{journey_id}",
            client_id=client_id,
            case_id=plan_id or journey_id,
            case_type="advisory_plan" if plan_id else "investment_journey",
            source_type="journey_activation",
            source_id=journey_id,
            event_type="advisory_plan.activated",
            output_summary=f"activated_at={activated_at}",
            payload={"activation_snapshot_version": activation_snapshot_version},
            subjects=[
                {"subject_type": "journey", "subject_id": journey_id, "relation": "referenced"},
            ] + ([{
                "subject_type": "advisory_plan",
                "subject_id": plan_id,
                "relation": "updated",
            }] if plan_id else []),
        )
    except Exception as exc:
        print(f"[activation] Failed to trace advisory activation: {exc}", flush=True)


__all__ = ["activate_journey_policy"]

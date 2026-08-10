"""Internal Cloud Tasks and scheduler route handlers."""

from __future__ import annotations

import os
from typing import Any, Callable, Tuple

from flask import Blueprint, jsonify, request


def _internal_secret_valid() -> bool:
    secret = os.getenv("CLOUD_TASKS_INTERNAL_SECRET", "")
    return not secret or request.headers.get("X-Internal-Secret") == secret


def create_tasks_blueprint(
    *,
    diagnosis_service_factory: Callable[[], Any],
    proactive_service_factory: Callable[[], Any],
    business_event_worker_factory: Callable[[], Any] | None = None,
    regular_consult_task_factory: Callable[[], Any] | None = None,
    advisor_runtime_factory: Callable[[], Any] | None = None,
    planning_refresh_coordinator_factory: Callable[[], Any] | None = None,
) -> Blueprint:
    """Create internal task routes with app-level dependency hooks."""
    bp = Blueprint("tasks", __name__)

    @bp.route("/internal/tasks/diagnosis-refresh", methods=["POST"])
    def internal_diagnosis_refresh_task() -> Tuple[Any, int]:
        if not _internal_secret_valid():
            return jsonify({"error": "unauthorized"}), 403

        body = request.get_json(silent=True) or {}
        client_id = str(body.get("client_id") or "").strip()
        specialist_job_id = str(body.get("specialist_job_id") or "").strip()
        if not client_id:
            return jsonify({"error": "client_id required"}), 400

        service = diagnosis_service_factory()
        result = service.run_queued_refresh(client_id)
        if specialist_job_id:
            from advisor.agents.background_jobs import (
                complete_external_specialist_job,
            )

            complete_external_specialist_job(
                specialist_job_id,
                result=result if result.get("status") != "failed" else None,
                error=(
                    str(result.get("error") or "Diagnosis refresh failed")
                    if result.get("status") == "failed"
                    else None
                ),
            )
            if result.get("status") == "superseded":
                service._schedule_refresh(client_id)
        http_status = 200 if result.get("status") != "failed" else 500
        return jsonify(result), http_status

    @bp.route("/internal/tasks/investment-solution", methods=["POST"])
    def internal_investment_solution_task() -> Tuple[Any, int]:
        if not _internal_secret_valid():
            return jsonify({"error": "unauthorized"}), 403
        if advisor_runtime_factory is None:
            return jsonify({"error": "advisor runtime is not configured"}), 503

        body = request.get_json(silent=True) or {}
        specialist_job_id = str(body.get("specialist_job_id") or "").strip()
        if not specialist_job_id:
            return jsonify({"error": "specialist_job_id required"}), 400

        from advisor.agents.background_jobs import (
            run_external_investment_solution_job,
        )

        runtime = advisor_runtime_factory()
        tool_executor = getattr(runtime, "_tool_executor", None)
        if tool_executor is None:
            return jsonify({"error": "advisor tool executor is not configured"}), 503
        try:
            event = run_external_investment_solution_job(
                specialist_job_id,
                tool_executor=tool_executor,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

        status = str((event or {}).get("status") or "")
        payload = (event or {}).get("payload")
        result = {
            "specialist_job_id": specialist_job_id,
            "status": status,
            "result": (
                payload.get("result")
                if isinstance(payload, dict) and status == "done"
                else None
            ),
            "error": (
                payload.get("error")
                if isinstance(payload, dict) and status == "failed"
                else None
            ),
        }
        return jsonify(result), 500 if status == "failed" else 200

    @bp.route("/internal/tasks/financial-planning", methods=["POST"])
    def internal_financial_planning_task() -> Tuple[Any, int]:
        if not _internal_secret_valid():
            return jsonify({"error": "unauthorized"}), 403
        if advisor_runtime_factory is None:
            return jsonify({"error": "advisor runtime is not configured"}), 503
        specialist_job_id = str(
            (request.get_json(silent=True) or {}).get("specialist_job_id") or ""
        ).strip()
        if not specialist_job_id:
            return jsonify({"error": "specialist_job_id required"}), 400
        from advisor.agents.background_jobs import run_external_financial_planning_job

        tool_executor = getattr(advisor_runtime_factory(), "_tool_executor", None)
        if tool_executor is None:
            return jsonify({"error": "advisor tool executor is not configured"}), 503
        try:
            event = run_external_financial_planning_job(
                specialist_job_id,
                tool_executor=tool_executor,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        status = str((event or {}).get("status") or "")
        payload = (event or {}).get("payload")
        return jsonify(
            {
                "specialist_job_id": specialist_job_id,
                "status": status,
                "result": payload.get("result") if isinstance(payload, dict) and status == "done" else None,
                "error": payload.get("error") if isinstance(payload, dict) and status == "failed" else None,
            }
        ), 500 if status == "failed" else 200

    @bp.route("/internal/tasks/proactive-evaluate", methods=["POST"])
    def internal_proactive_evaluate_task() -> Tuple[Any, int]:
        if not _internal_secret_valid():
            return jsonify({"error": "unauthorized"}), 403

        body = request.get_json(silent=True) or {}
        client_id = str(body.get("client_id") or "").strip()
        service = proactive_service_factory()

        if client_id:
            result = service.evaluate_and_compose(client_id)
            http_status = 200 if result.error is None else 500
            return (
                jsonify(
                    {
                        "mode": "single",
                        "result": result.to_dict(),
                    }
                ),
                http_status,
            )

        results = service.evaluate_all_clients()
        triggered_count = sum(1 for r in results if r.triggered)
        error_count = sum(1 for r in results if r.error)
        return (
            jsonify(
                {
                    "mode": "batch",
                    "total": len(results),
                    "triggered": triggered_count,
                    "errors": error_count,
                    "results": [r.to_dict() for r in results],
                }
            ),
            200,
        )

    @bp.route("/internal/tasks/business-events-drain", methods=["POST"])
    def internal_business_events_drain_task() -> Tuple[Any, int]:
        if not _internal_secret_valid():
            return jsonify({"error": "unauthorized"}), 403
        if business_event_worker_factory is None:
            return jsonify({"error": "business event worker is not configured"}), 503

        body = request.get_json(silent=True) or {}
        try:
            limit = int(body.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20

        event_type = str(body.get("event_type") or "").strip() or None
        result = business_event_worker_factory().drain(
            limit=limit, event_type=event_type
        )
        http_status = 200 if int(result.get("failed") or 0) == 0 else 207
        return jsonify(result), http_status

    @bp.route("/internal/tasks/planning-refresh-sweep", methods=["POST"])
    def internal_planning_refresh_sweep() -> Tuple[Any, int]:
        if not _internal_secret_valid():
            return jsonify({"error": "unauthorized"}), 403
        if planning_refresh_coordinator_factory is None:
            return jsonify({"error": "planning refresh coordinator is not configured"}), 503
        body = request.get_json(silent=True) or {}
        try:
            limit = max(1, min(int(body.get("limit", 20)), 100))
        except (TypeError, ValueError):
            return jsonify({"error": "limit must be an integer"}), 400
        results = planning_refresh_coordinator_factory().sweep(
            limit=limit,
            client_id=str(body.get("client_id") or "").strip() or None,
        )
        return jsonify({"success": True, "reserved": len(results), "results": results}), 200

    @bp.route("/internal/tasks/planning-refresh", methods=["POST"])
    def internal_planning_refresh() -> Tuple[Any, int]:
        if not _internal_secret_valid():
            return jsonify({"error": "unauthorized"}), 403
        if planning_refresh_coordinator_factory is None:
            return jsonify({"error": "planning refresh coordinator is not configured"}), 503
        body = request.get_json(silent=True) or {}
        required = ("client_id", "source_client_version", "active_job_id")
        if any(not body.get(key) for key in required):
            return jsonify({"error": "client_id, source_client_version, and active_job_id are required"}), 400
        try:
            result = planning_refresh_coordinator_factory().run_reserved({
                "client_id": str(body["client_id"]),
                "source_client_version": int(body["source_client_version"]),
                "active_job_id": str(body["active_job_id"]),
            })
        except Exception as exc:
            return jsonify({"status": "failed", "error": str(exc)}), 500
        return jsonify(result), 200

    @bp.route("/internal/tasks/v2/regular-consult", methods=["POST"])
    def internal_advisor_regular_consult_task() -> Tuple[Any, int]:
        if not _internal_secret_valid():
            return jsonify({"error": "unauthorized"}), 403
        if regular_consult_task_factory is None:
            return jsonify({"error": "regular consult task is not configured"}), 503

        from advisor.proactive.objectives import (
            regular_consult_task_config_from_request,
        )

        body = request.get_json(silent=True) or {}
        config = regular_consult_task_config_from_request(body)
        result = regular_consult_task_factory().run(config)
        return jsonify(result), 200

    return bp


__all__ = ["create_tasks_blueprint"]

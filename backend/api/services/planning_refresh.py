"""Recoverable, leased orchestration for advisor-owned planning refreshes."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from client_file.interfaces import ClientFileReader


PlanningRunner = Callable[[str, int, Dict[str, Any]], Dict[str, Dict[str, Any]]]


class PlanningRefreshCoordinator:
    def __init__(
        self,
        *,
        client_file_reader: ClientFileReader,
        planning_runner: Optional[PlanningRunner] = None,
        dispatcher: Optional[Callable[..., Dict[str, Any]]] = None,
    ) -> None:
        self.client_file_reader = client_file_reader
        self.planning_runner = planning_runner or _run_financial_planning
        self.dispatcher = dispatcher or _dispatch_background

    def handle_client_file_updated(self, event: Dict[str, Any]) -> Dict[str, Any]:
        from api.persistence import request_planning_refresh

        client_id = str(event.get("client_id") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        version = int(payload.get("version") or 0)
        if not client_id or version <= 0:
            return {"action": "skipped", "reason": "missing client_id or version"}
        request_planning_refresh(
            client_id=client_id,
            version=version,
            source_input_fingerprint=payload.get("source_input_fingerprint"),
        )
        reservations = self.sweep(limit=1, client_id=client_id)
        if not reservations:
            from api.persistence import get_planning_refresh_state

            state = get_planning_refresh_state(client_id=client_id)
            return {
                "action": "planning_refresh_deferred",
                "reason": "consultation_active" if state.get("consultation_active") else state.get("status"),
                "version": state.get("latest_requested_version") or version,
            }
        return reservations[0]

    def consultation_state_changed(self, *, client_id: str, active: bool) -> Dict[str, Any]:
        from api.persistence import get_planning_refresh_state, set_planning_consultation_active

        state = set_planning_consultation_active(client_id=client_id, active=active)
        if active or not state.get("dirty"):
            return {"action": "consultation_state_updated", "state": state}
        latest = get_planning_refresh_state(client_id=client_id)
        results = self.sweep(limit=1, client_id=client_id)
        return results[0] if results else {
            "action": "planning_refresh_deferred",
            "reason": latest.get("status"),
            "version": latest.get("latest_requested_version"),
        }

    def sweep(self, *, limit: int = 20, client_id: Optional[str] = None) -> list[Dict[str, Any]]:
        """Reserve and dispatch durable work even when its wake event is gone."""

        from api.persistence import release_planning_refresh_claim, reserve_planning_refreshes

        reservations = reserve_planning_refreshes(limit=limit, client_id=client_id)
        dispatched = []
        for reservation in reservations:
            try:
                receipt = self.dispatcher(
                    specialist_key="planning_refresh",
                    client_id=reservation["client_id"],
                    objective=f"Refresh planning artifacts for Client File v{reservation['source_client_version']}",
                    callback=lambda reserved=dict(reservation): self.run_reserved(reserved),
                    event_source="planning_refresh.sweep",
                    client_file_version_at_start=reservation["source_client_version"],
                )
            except Exception as exc:
                release_planning_refresh_claim(
                    client_id=reservation["client_id"],
                    version=reservation["source_client_version"],
                    job_id=reservation["active_job_id"],
                    error=f"planning_dispatch_failed:{exc}",
                )
                raise
            dispatched.append({
                "action": "planning_refresh_dispatched",
                "version": reservation["source_client_version"],
                "active_job_id": reservation["active_job_id"],
                "job": receipt,
            })
        return dispatched

    def run_reserved(self, reservation: Dict[str, Any]) -> Dict[str, Any]:
        from api.persistence import (
            begin_planning_refresh,
            block_planning_artifact_set,
            create_planning_artifact_set,
            fail_planning_artifact_set,
            publish_planning_refresh,
            release_planning_refresh_claim,
        )

        client_id = str(reservation["client_id"])
        version = int(reservation["source_client_version"])
        job_id = str(reservation["active_job_id"])
        begun = begin_planning_refresh(client_id=client_id, version=version, job_id=job_id)
        if not begun.get("started"):
            return {"status": "duplicate", "reason": begun.get("reason"), "active_job_id": job_id}

        snapshot = self.client_file_reader.read(client_id).payload
        if int(snapshot.get("client_file_version") or 0) != version:
            release_planning_refresh_claim(
                client_id=client_id, version=version, job_id=job_id,
                error="client_file_version_superseded",
            )
            return self._run_next_if_eligible(client_id, [{"status": "stale", "source_client_version": version}])
        input_snapshot = build_financial_input_snapshot(client_id=client_id, client_file=snapshot)
        artifact_set = create_planning_artifact_set(
            client_id=client_id, source_version=version,
            active_job_id=job_id, source_snapshot=input_snapshot,
        )
        try:
            artifacts = self.planning_runner(client_id, version, {**snapshot, "financial_input_snapshot": input_snapshot})
            if artifacts.get("blocked") is True:
                question = str(artifacts.get("next_question") or "Please provide the missing planning inputs.")
                block_planning_artifact_set(set_id=str(artifact_set["id"]), question=question)
                release_planning_refresh_claim(
                    client_id=client_id, version=version, job_id=job_id,
                    error=question, retryable=False, blocked=True,
                )
                return {"status": "input_required", "next_question": question, "artifact_sets": [{**artifact_set, "status": "blocked", "published": False, "missing_required_inputs": artifacts.get("missing_required_inputs") or []}]}
            publication = publish_planning_refresh(
                client_id=client_id, version=version, job_id=job_id,
                set_id=str(artifact_set["id"]),
                source_input_fingerprint=input_snapshot["source_input_fingerprint"],
                artifacts=artifacts,
            )
        except Exception as exc:
            fail_planning_artifact_set(set_id=str(artifact_set["id"]), error=str(exc))
            release_planning_refresh_claim(
                client_id=client_id, version=version, job_id=job_id,
                error=str(exc), retryable=True,
            )
            raise
        if publication.get("published"):
            return {"status": "done", "artifact_sets": [publication]}
        return self._run_next_if_eligible(client_id, [publication])

    # Backward-compatible entry used by earlier tests and internal callers.
    def _run_until_current(self, client_id: str, version: int) -> Dict[str, Any]:
        from api.persistence import get_planning_refresh_state, reserve_planning_refreshes

        state = get_planning_refresh_state(client_id=client_id)
        reservation = {
            "client_id": client_id,
            "source_client_version": version,
            "active_job_id": state.get("active_job_id"),
        }
        if not reservation["active_job_id"]:
            reserved = reserve_planning_refreshes(limit=1, client_id=client_id)
            if not reserved:
                return {"status": "deferred", "reason": state.get("status"), "artifact_sets": []}
            reservation = reserved[0]
        return self.run_reserved(reservation)

    def _run_next_if_eligible(self, client_id: str, completed: list[Dict[str, Any]]) -> Dict[str, Any]:
        from api.persistence import reserve_planning_refreshes

        reservations = reserve_planning_refreshes(limit=1, client_id=client_id)
        if not reservations:
            return {"status": "deferred", "artifact_sets": completed}
        next_result = self.run_reserved(reservations[0])
        return {**next_result, "artifact_sets": completed + list(next_result.get("artifact_sets") or [])}


def build_financial_input_snapshot(*, client_id: str, client_file: Dict[str, Any]) -> Dict[str, Any]:
    """Freeze the complete deterministic input identity used by all phases."""
    from client_file.financial_position import resolve_financial_position

    position = resolve_financial_position(client_id=client_id, client_file=client_file)
    return {
        **position,
        "schema_version": "financial_input_snapshot.v1",
        "resolved_operands": position["net_worth_operands"],
    }


def _dispatch_background(**kwargs: Any) -> Dict[str, Any]:
    from advisor.agents.background_jobs import dispatch_background_callable

    return dispatch_background_callable(**kwargs)


def _run_financial_planning(client_id: str, version: int, client_file: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    from advisor.pipelines.planning_refresh import PlanningRefreshPipeline

    snapshot = client_file.get("financial_input_snapshot") or build_financial_input_snapshot(
        client_id=client_id,
        client_file={**client_file, "client_file_version": version},
    )
    return PlanningRefreshPipeline().run(
        client_id=client_id,
        snapshot=snapshot,
        client_file=client_file,
        prior_planning_context=client_file.get("current_planning_set"),
    )


def planning_is_fresh(client_file: Dict[str, Any]) -> bool:
    from client_file.lifecycle import planning_is_fresh as _planning_is_fresh

    return _planning_is_fresh(client_file)

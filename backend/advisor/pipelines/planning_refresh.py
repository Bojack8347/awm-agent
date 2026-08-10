"""Advisor-owned semantic pipeline for one immutable planning input snapshot."""

from __future__ import annotations

from typing import Any, Dict, Optional


class PlanningRefreshPipeline:
    def run(
        self,
        *,
        client_id: str,
        snapshot: Dict[str, Any],
        client_file: Dict[str, Any],
        prior_planning_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        from advisor.tools.deterministic_tools.execution import build_production_subagents
        from advisor.tools.deterministic_tools.run_cashflow_projection.scenarios import cashflow_required_metrics

        version = int(snapshot.get("source_client_file_version") or 0)
        specialist, _investment, _revalidation = build_production_subagents()
        knowledge_artifact = specialist.build_artifact(
            client_id=client_id,
            objective={
                "request": "Refresh complete financial plan",
                "client_file_version": version,
                "financial_input_snapshot_id": snapshot.get("snapshot_id"),
            },
            client_file=client_file,
        )
        knowledge = knowledge_artifact.payload
        diagnosis = {
            "client_id": client_id,
            "source_client_version": version,
            "status": knowledge.get("status"),
            "findings": knowledge.get("findings") or [],
            "missing_data": knowledge.get("missing_data") or [],
            "summary": knowledge.get("summary") or {},
            "source": "financial_planning_agent_v2",
        }
        projection = specialist.run_cashflow_projection(
            client_id=client_id,
            session_id=f"planning-refresh-v{version}",
            question="Refresh the canonical baseline financial projection.",
            scenario={
                "requested": True,
                "action": "run_cashflow_model",
                "confidence": 1.0,
                "source": "planning_refresh",
                "reason": "Canonical planning inputs changed.",
                "evidence": [str(snapshot.get("snapshot_id") or f"client_file_version:{version}")],
                "requested_metrics": cashflow_required_metrics(),
                "scenario_changes": [],
                "negated": False,
            },
            client_file=client_file,
        )
        execution = (projection.get("status") or {}).get("execution")
        if execution not in {"completed", "success", "succeeded"}:
            if (projection.get("status") or {}).get("error") == "missing_required_inputs":
                return {
                    "blocked": True,
                    "next_question": projection.get("next_question"),
                    "missing_required_inputs": projection.get("missing_data") or [],
                }
            raise RuntimeError(
                "financial planning projection unavailable: "
                + str((projection.get("status") or {}).get("error") or "not_run")
            )
        evidence = {
            "source_snapshot_id": snapshot.get("snapshot_id"),
            "source_input_fingerprint": snapshot.get("source_input_fingerprint"),
            "source_provider_revisions": snapshot.get("source_provider_revisions") or [],
            "source_client_version": version,
        }
        return {
            "knowledge": {**knowledge, **evidence},
            "diagnosis": {**diagnosis, **evidence},
            "projection": {**projection, **evidence},
        }

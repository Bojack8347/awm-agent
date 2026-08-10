"""Materialize advisor investment results into APP proposal and policy rows."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

from api.services.asset_allocation_artifact_adapter import (
    proposal_artifact_from_advisor_policy,
    proposal_artifact_from_allocation_result,
)


@dataclass(frozen=True)
class ProposalMaterializationResult:
    records: List[Dict[str, Any]]
    outcomes: List[Dict[str, Any]]

    def __iter__(
        self,
    ) -> Iterator[List[Dict[str, Any]]]:
        yield self.records
        yield self.outcomes


def _deterministic_id(prefix: str, *parts: str) -> str:
    identity = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def ready_proposals_for_assessment(
    *,
    deps: Any,
    client_id: str,
    assessment_id: str,
) -> List[Dict[str, Any]]:
    """Return persisted ready proposals for an already-signed assessment."""

    clean_assessment_id = str(assessment_id or "").strip()
    if not clean_assessment_id or not callable(
        getattr(deps, "db_list_artifacts", None)
    ):
        return []
    matches: List[Dict[str, Any]] = []
    for artifact in deps.db_list_artifacts(
        client_id=client_id,
        artifact_type="proposal",
    ) or []:
        if not isinstance(artifact, dict):
            continue
        payload = artifact.get("payload")
        if not isinstance(payload, dict):
            continue
        source_assessment = (
            payload.get("source_assessment")
            if isinstance(payload.get("source_assessment"), dict)
            else {}
        )
        proposal_assessment_ids = {
            str(payload.get("assessment_id") or "").strip(),
            str(payload.get("source_assessment_id") or "").strip(),
            str(source_assessment.get("assessment_id") or "").strip(),
        }
        if clean_assessment_id not in proposal_assessment_ids:
            continue
        engine_run = (
            payload.get("engine_run")
            if isinstance(payload.get("engine_run"), dict)
            else {}
        )
        record_ready = str(artifact.get("status") or "").strip().lower() == "ready"
        payload_status = str(payload.get("artifact_status") or "").strip().lower()
        payload_ready = not payload_status or payload_status == "ready"
        engine_succeeded = (
            str(engine_run.get("status") or "").strip().lower() == "succeeded"
        )
        if record_ready and payload_ready and engine_succeeded:
            matches.append(artifact)
    return matches


def materialize_proposal_artifacts(
    *,
    deps: Any,
    client_id: str,
    session_id: str,
    result: Dict[str, Any],
) -> ProposalMaterializationResult:
    """Bridge ready advisor policies into the APP-facing proposal store."""

    persisted: List[Dict[str, Any]] = []
    outcomes: List[Dict[str, Any]] = []
    proposal_payloads: List[Dict[str, Any]] = []
    allocation_results = {
        str(tool_result.get("analysis_id") or ""): tool_result
        for tool_result in (result.get("tool_results") or [])
        if isinstance(tool_result, dict)
        and tool_result.get("tool") == "run_asset_allocation"
        and tool_result.get("ok") is True
        and str(tool_result.get("analysis_id") or "")
    }
    subagent_artifacts = [
        artifact
        for artifact in result.get("subagent_artifacts") or []
        if isinstance(artifact, dict)
    ]
    proposal_candidates = [
        artifact
        for artifact in subagent_artifacts
        if proposal_artifact_from_advisor_policy(
            artifact,
            allocation_result=None,
        )
        is not None
    ]
    for subagent_artifact in proposal_candidates:
        advisor_payload = (
            subagent_artifact.get("payload")
            if isinstance(subagent_artifact.get("payload"), dict)
            else {}
        )
        advisor_engine_run = (
            advisor_payload.get("engine_run")
            if isinstance(advisor_payload.get("engine_run"), dict)
            else {}
        )
        allocation_result = allocation_results.get(
            str(advisor_engine_run.get("analysis_id") or "")
        )
        if (
            allocation_result is None
            and len(allocation_results) == 1
            and len(proposal_candidates) == 1
        ):
            allocation_result = next(iter(allocation_results.values()))
        proposal_payload = proposal_artifact_from_advisor_policy(
            subagent_artifact,
            allocation_result=allocation_result,
        )
        if proposal_payload is not None:
            proposal_payloads.append(proposal_payload)

    if allocation_results and callable(
        getattr(deps, "build_client_state_view", None)
    ):
        client_state = deps.build_client_state_view(client_id)
        for allocation_result in allocation_results.values():
            proposal_payload = proposal_artifact_from_allocation_result(
                allocation_result,
                client_state=(
                    client_state if isinstance(client_state, dict) else {}
                ),
            )
            if proposal_payload is not None:
                proposal_payloads.append(proposal_payload)

    unique_payloads: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()
    for proposal_payload in proposal_payloads:
        source_assessment = (
            proposal_payload.get("source_assessment")
            if isinstance(proposal_payload.get("source_assessment"), dict)
            else {}
        )
        dedupe_key = str(
            proposal_payload.get("source_allocation_analysis_id")
            or proposal_payload.get("source_advisor_artifact_id")
            or (
                f"{source_assessment.get('assessment_id')}:"
                f"{source_assessment.get('assessment_version')}:"
                f"{source_assessment.get('money_pool_id')}"
            )
        )
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        unique_payloads.append(proposal_payload)

    for proposal_payload in unique_payloads:
        started = time.perf_counter()
        source_id = str(
            proposal_payload.get("source_advisor_artifact_id") or ""
        ).strip()
        money_pool = (
            proposal_payload.get("money_pool")
            if isinstance(proposal_payload.get("money_pool"), dict)
            else {}
        )
        pool_label = str(money_pool.get("label") or "").strip()
        outcome: Dict[str, Any] = {
            "source_advisor_artifact_id": source_id,
            "money_pool_label": pool_label,
        }
        try:
            allocation_id = str(
                proposal_payload.get("source_allocation_analysis_id") or ""
            ).strip()
            source_assessment = (
                proposal_payload.get("source_assessment")
                if isinstance(proposal_payload.get("source_assessment"), dict)
                else {}
            )
            assessment_id = str(
                proposal_payload.get("assessment_id")
                or source_assessment.get("assessment_id")
                or ""
            ).strip()
            assessment_version = (
                proposal_payload.get("assessment_version")
                or source_assessment.get("assessment_version")
                or 1
            )
            money_pool_id = str(
                proposal_payload.get("money_pool_id")
                or source_assessment.get("money_pool_id")
                or money_pool.get("id")
                or ""
            ).strip()

            if allocation_id and callable(
                getattr(deps, "db_save_asset_allocation_proposal_bundle", None)
            ):
                policy_payload = _build_proposed_policy_payload(
                    proposal_payload=proposal_payload,
                    proposal_id="",
                )
                bundle = deps.db_save_asset_allocation_proposal_bundle(
                    client_id=client_id,
                    idempotency_key=(
                        f"assessment:{assessment_id}:v{assessment_version}:"
                        f"allocation:{allocation_id}"
                    ),
                    artifact_title=proposal_payload["title"],
                    artifact_payload=proposal_payload,
                    policy_payload=policy_payload,
                    money_pool_id=money_pool_id or None,
                )
                if not isinstance(bundle, dict) or bundle.get("ok") is not True:
                    raise RuntimeError(
                        str(
                            bundle.get("error")
                            if isinstance(bundle, dict)
                            else "proposal bundle persistence returned no result"
                        )
                    )
                saved = bundle.get("artifact")
                policy = bundle.get("policy")
                if not isinstance(saved, dict) or not isinstance(policy, dict):
                    raise RuntimeError(
                        "proposal bundle persistence did not return both records"
                    )
                persisted.append(saved)
                outcome.update(
                    {
                        "status": "success",
                        "operation": (
                            "reused"
                            if bundle.get("idempotent_replay") is True
                            else "created"
                        ),
                        "artifact_id": saved.get("id"),
                        "policy_id": policy.get("id"),
                    }
                )
                outcome["duration_ms"] = round(
                    (time.perf_counter() - started) * 1000,
                    3,
                )
                outcomes.append(outcome)
                continue

            if not source_id:
                raise RuntimeError("source advisor artifact id is required")
            existing = None
            candidates = deps.db_list_artifacts(
                client_id=client_id,
                artifact_type="proposal",
            ) or []
            for candidate in candidates:
                candidate_payload = (
                    candidate.get("payload")
                    if isinstance(candidate.get("payload"), dict)
                    else {}
                )
                candidate_pool = (
                    candidate_payload.get("money_pool")
                    if isinstance(candidate_payload.get("money_pool"), dict)
                    else {}
                )
                same_source = (
                    str(
                        candidate_payload.get("source_advisor_artifact_id") or ""
                    )
                    == source_id
                )
                # The pool fallback updates an existing non-draft proposal when
                # the advisor regenerates it under a new source artifact id.
                same_pool = (
                    bool(pool_label)
                    and str(candidate_pool.get("label") or "").strip().lower()
                    == pool_label.lower()
                )
                if same_source or (
                    same_pool and str(candidate.get("status") or "") != "draft"
                ):
                    existing = candidate
                    break

            if existing is not None and callable(
                getattr(deps, "db_update_artifact", None)
            ):
                saved = deps.db_update_artifact(
                    artifact_id=existing["id"],
                    client_id=client_id,
                    status="ready",
                    payload_patch=proposal_payload,
                )
                operation = "updated"
            elif existing is not None:
                saved = existing
                operation = "reused"
            else:
                saved = deps.db_save_artifact(
                    artifact_id=_deterministic_id(
                        "proposal",
                        client_id,
                        source_id,
                    ),
                    client_id=client_id,
                    artifact_type="proposal",
                    title=proposal_payload["title"],
                    payload=proposal_payload,
                    related_type="advisor_session",
                    related_id=session_id,
                    status="ready",
                )
                operation = "created"

            if not isinstance(saved, dict):
                raise RuntimeError("artifact persistence returned no record")
            policy = persist_proposed_policy_from_proposal(
                deps=deps,
                client_id=client_id,
                proposal_record=saved,
                proposal_payload=proposal_payload,
            )
            persisted.append(saved)
            outcome.update(
                {
                    "status": "success",
                    "operation": operation,
                    "artifact_id": saved.get("id"),
                    "policy_id": (
                        policy.get("id") if isinstance(policy, dict) else None
                    ),
                }
            )
        except Exception as exc:  # pylint: disable=broad-except
            outcome.update(
                {"status": "error", "operation": "failed", "error": str(exc)}
            )
        outcome["duration_ms"] = round(
            (time.perf_counter() - started) * 1000,
            3,
        )
        outcomes.append(outcome)
    return ProposalMaterializationResult(persisted, outcomes)


def persist_proposed_policy_from_proposal(
    *,
    deps: Any,
    client_id: str,
    proposal_record: Dict[str, Any],
    proposal_payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Persist the reviewable Policy-tab row from one complete proposal."""

    if not callable(getattr(deps, "db_list_policies", None)) or not callable(
        getattr(deps, "db_save_policy", None)
    ):
        return None
    artifact_id = str(proposal_record.get("id") or "").strip()
    if not artifact_id:
        raise RuntimeError(
            "complete proposal audit fields are required before policy persistence"
        )
    policy_payload = _build_proposed_policy_payload(
        proposal_payload=proposal_payload,
        proposal_id=artifact_id,
    )
    source_assessment = policy_payload["source_assessment"]
    policy = policy_payload["policy"]
    assessment_id = str(source_assessment["assessment_id"])
    existing = None
    for candidate in deps.db_list_policies(client_id=client_id, status=None) or []:
        candidate_payload = (
            candidate.get("payload")
            if isinstance(candidate, dict)
            and isinstance(candidate.get("payload"), dict)
            else {}
        )
        candidate_policy = (
            candidate_payload.get("policy")
            if isinstance(candidate_payload.get("policy"), dict)
            else {}
        )
        candidate_assessment = (
            candidate_payload.get("source_assessment")
            if isinstance(candidate_payload.get("source_assessment"), dict)
            else {}
        )
        same_proposal = (
            str(candidate_payload.get("proposal_id") or candidate.get("related_id") or "")
            == artifact_id
        )
        same_assessment = (
            str(
                candidate_policy.get("source_assessment_id")
                or candidate_assessment.get("assessment_id")
                or ""
            )
            == assessment_id
        )
        if same_proposal or same_assessment:
            existing = candidate
            break

    if existing is not None and callable(getattr(deps, "db_update_policy", None)):
        return deps.db_update_policy(
            policy_id=existing["id"],
            client_id=client_id,
            status="proposed",
            payload_patch=policy_payload,
        )
    if existing is not None:
        return existing
    return deps.db_save_policy(
        policy_id=_deterministic_id(
            "policy",
            client_id,
            assessment_id,
        ),
        client_id=client_id,
        status="proposed",
        proposal_id=artifact_id,
        payload=policy_payload,
    )


def _build_proposed_policy_payload(
    *,
    proposal_payload: Dict[str, Any],
    proposal_id: str,
) -> Dict[str, Any]:
    """Validate and project one complete proposal into the Policy read model."""

    source_assessment = (
        proposal_payload.get("source_assessment")
        if isinstance(proposal_payload.get("source_assessment"), dict)
        else {}
    )
    policy = (
        proposal_payload.get("policy")
        if isinstance(proposal_payload.get("policy"), dict)
        else {}
    )
    securities = (
        proposal_payload.get("recommended_securities")
        if isinstance(proposal_payload.get("recommended_securities"), list)
        else policy.get("recommended_securities")
        if isinstance(policy.get("recommended_securities"), list)
        else []
    )
    assessment_id = str(
        proposal_payload.get("assessment_id")
        or source_assessment.get("assessment_id")
        or policy.get("source_assessment_id")
        or ""
    ).strip()
    if not assessment_id or not securities:
        raise RuntimeError(
            "complete proposal audit fields are required before policy persistence"
        )

    return {
        **proposal_payload,
        "artifact_type": "investment_policy_proposal",
        "artifact_status": "ready",
        "schema_version": "investment_policy_proposal.v1",
        "proposal_id": proposal_id,
        "title": proposal_payload.get("title") or policy.get("title"),
        "status": "proposed",
        "source_assessment": {
            **source_assessment,
            "assessment_id": assessment_id,
            "status": "signed_off",
        },
        "policy": {
            **policy,
            "status": "proposed",
            "review_status": "pending",
            "source_assessment_id": assessment_id,
            "recommended_securities": securities,
        },
        "recommended_securities": securities,
    }


__all__ = [
    "ProposalMaterializationResult",
    "materialize_proposal_artifacts",
    "persist_proposed_policy_from_proposal",
    "ready_proposals_for_assessment",
]

from __future__ import annotations

from typing import Any, Dict


def _client_file_snapshot_version(client_file: Dict[str, Any]) -> Any:
    facts = (
        client_file.get("facts")
        if isinstance(client_file.get("facts"), dict)
        else {}
    )
    return (
        facts.get("knowledge_snapshot_version")
        or client_file.get("snapshot_version")
        or client_file.get("version")
    )


def _durable_client_file_contains_commit(
    client_file: Dict[str, Any],
    commit_result: Dict[str, Any],
) -> bool:
    payload = (
        commit_result.get("payload")
        if isinstance(commit_result.get("payload"), dict)
        else {}
    )
    committed_facts = (
        payload.get("facts")
        if isinstance(payload.get("facts"), dict)
        else {}
    )
    if not committed_facts:
        return False
    durable_facts = (
        client_file.get("facts")
        if isinstance(client_file.get("facts"), dict)
        else {}
    )
    if any(durable_facts.get(key) != value for key, value in committed_facts.items()):
        return False
    committed_structured = (
        payload.get("structured_facts")
        if isinstance(payload.get("structured_facts"), dict)
        else {}
    )
    durable_structured = (
        client_file.get("structured_facts")
        if isinstance(client_file.get("structured_facts"), dict)
        else {}
    )
    return all(
        durable_structured.get(key) == value
        for key, value in committed_structured.items()
    )


def _client_file_ready_for_assessment_presentation(client_file: Dict[str, Any]) -> bool:
    """Pool is ready and assessment is either missing or still pending unsigned."""

    if not _client_file_has_assessment_ready_pool(client_file):
        return False
    if _client_file_has_signed_assessment(client_file):
        return False
    return True


def _client_file_has_proposed_policy(client_file: Dict[str, Any]) -> bool:
    if not isinstance(client_file, dict):
        return False
    for proposal_key in ("proposals", "investment_proposals"):
        proposals = client_file.get(proposal_key)
        if isinstance(proposals, list) and any(
            isinstance(item, dict) for item in proposals
        ):
            return True
    policies = client_file.get("policies")
    if isinstance(policies, dict):
        for policy_key in ("proposed", "mvp", "active"):
            policy_rows = policies.get(policy_key)
            if isinstance(policy_rows, list) and any(
                isinstance(item, dict) for item in policy_rows
            ):
                return True
    return False


def _client_file_ready_for_proposal_construction(client_file: Dict[str, Any]) -> bool:
    """Durable gate: signed assessment exists and no proposed policy is on file yet."""

    return _client_file_has_signed_assessment(
        client_file
    ) and not _client_file_has_proposed_policy(client_file)


def _money_pool_is_assessment_ready(pool: Any) -> bool:
    if not isinstance(pool, dict):
        return False
    missing = pool.get("missing_fields")
    if isinstance(missing, list) and missing:
        return False
    if pool.get("amount") in (None, ""):
        return False
    if not pool.get("risk_tolerance"):
        return False
    if not (
        pool.get("purpose_type")
        or pool.get("purpose")
        or pool.get("objective")
    ):
        return False
    if not (
        pool.get("horizon_date")
        or pool.get("horizon_text")
        or pool.get("horizon_years") not in (None, "")
    ):
        return False
    state = str(pool.get("state") or "").strip().lower()
    return state in {"", "defined", "ready", "complete", "completed"}


def _client_file_has_assessment_ready_pool(client_file: Dict[str, Any]) -> bool:
    if not isinstance(client_file, dict):
        return False
    pools = client_file.get("money_pools")
    if isinstance(pools, dict):
        pools = pools.get("pools")
    if not isinstance(pools, list):
        return False
    return any(_money_pool_is_assessment_ready(pool) for pool in pools)


def _client_file_has_pending_or_signed_assessment(client_file: Dict[str, Any]) -> bool:
    if _client_file_has_signed_assessment(client_file):
        return True
    if not isinstance(client_file, dict):
        return False
    for key in ("investment_assessments", "signed_investment_assessments"):
        assessments = client_file.get(key)
        if not isinstance(assessments, list):
            continue
        for assessment in assessments:
            if not isinstance(assessment, dict):
                continue
            if assessment.get("assessment_id") or assessment.get("id"):
                return True
            status = str(
                assessment.get("status")
                or assessment.get("assessment_status")
                or ""
            ).strip().lower()
            if status:
                return True
    return False


def _client_file_ready_for_assessment_creation(client_file: Dict[str, Any]) -> bool:
    """Durable gate: defined money pool exists and no assessment is on file yet."""

    return _client_file_has_assessment_ready_pool(
        client_file
    ) and not _client_file_has_pending_or_signed_assessment(client_file)


def _client_file_has_signed_assessment(client_file: Dict[str, Any]) -> bool:
    if not isinstance(client_file, dict):
        return False

    for key in ("investment_assessments", "signed_investment_assessments"):
        assessments = client_file.get(key)
        if not isinstance(assessments, list):
            continue
        for assessment in assessments:
            if _assessment_payload_is_signed(assessment):
                return True

    recent_writebacks = client_file.get("recent_writebacks")
    if isinstance(recent_writebacks, list):
        for writeback in recent_writebacks[:12]:
            if not isinstance(writeback, dict):
                continue
            if str(writeback.get("operation") or "") != "record_assessment_signoff":
                continue
            values = writeback.get("values") if isinstance(writeback.get("values"), dict) else {}
            if _assessment_payload_is_signed(values):
                return True

    open_loops = client_file.get("open_loops")
    if isinstance(open_loops, list):
        for loop in open_loops:
            if not isinstance(loop, dict):
                continue
            loop_type = str(loop.get("type") or "")
            status = str(loop.get("status") or "").lower()
            if "assessment_signoff" in loop_type and status in {"complete", "completed", "signed_off", "resolved"}:
                return True
    return False


def _assessment_payload_is_signed(assessment: Any) -> bool:
    if not isinstance(assessment, dict):
        return False
    if assessment.get("signed_off") is True:
        return True
    status = str(assessment.get("status") or assessment.get("assessment_status") or "").strip().lower()
    if status in {"signed_off", "approved", "confirmed"}:
        return True
    signoff = assessment.get("signoff") if isinstance(assessment.get("signoff"), dict) else {}
    return signoff.get("signed_off") is True

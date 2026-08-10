"""Deterministic, lineage-preserving investment-assessment sign-off."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from advisor.tools.deterministic_tools.investment_assessment_contract import (
    prefer_first_assessment_by_identity,
    validate_assessment_content_fingerprint,
    validate_assessment_eligibility,
)


PENDING_ASSESSMENT_STATUSES = {
    "pending_client_signoff",
    "pending_signoff",
    "awaiting_signoff",
    "ready_for_signoff",
}
SIGNED_ASSESSMENT_STATUSES = {"signed_off", "approved", "confirmed"}
DECLINED_ASSESSMENT_STATUSES = {"declined", "rejected", "cancelled", "canceled"}


def prepare_assessment_signoff(
    arguments: Dict[str, Any],
    client_file: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Resolve the requested pending assessment and stamp consent server-side."""
    decision = arguments.get("signed_off")
    if not isinstance(decision, bool):
        return {"ok": False, "error": "explicit_assessment_decision_required"}
    assessment_id = str(arguments.get("assessment_id") or "").strip()
    money_pool_id = str(arguments.get("money_pool_id") or "").strip()
    version = arguments.get("assessment_version")
    if (
        not assessment_id
        or not money_pool_id
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version < 1
    ):
        return {"ok": False, "error": "assessment_ref_invalid"}

    matches = [
        candidate
        for candidate in _assessment_candidates(client_file)
        if _assessment_id(candidate) == assessment_id
        and _assessment_version(candidate) == version
        and _money_pool_id(candidate) == money_pool_id
    ]
    if not matches:
        return {"ok": False, "error": "pending_assessment_not_found"}

    signed_matches = [
        candidate
        for candidate in matches
        if _assessment_status(candidate) in SIGNED_ASSESSMENT_STATUSES
        or candidate.get("signed_off") is True
    ]
    if signed_matches:
        fingerprints = {
            str(candidate.get("content_fingerprint") or "").strip()
            for candidate in signed_matches
            if str(candidate.get("content_fingerprint") or "").strip()
        }
        if fingerprints:
            if len(fingerprints) != 1:
                return {"ok": False, "error": "signed_assessment_ambiguous"}
        else:
            canonical_signed = {
                _canonical_assessment_json(candidate)
                for candidate in signed_matches
            }
            if len(canonical_signed) != 1:
                return {"ok": False, "error": "signed_assessment_ambiguous"}
        signed_candidate = next(
            (
                candidate
                for candidate in signed_matches
                if candidate.get("durable_artifact_id")
            ),
            signed_matches[0],
        )
        fingerprint_valid, fingerprint_error, fingerprint_details = (
            validate_assessment_content_fingerprint(signed_candidate)
        )
        if not fingerprint_valid:
            return {
                "ok": False,
                "error": fingerprint_error or "assessment_content_fingerprint_invalid",
                "details": fingerprint_details,
            }
        return {
            "ok": True,
            "payload": copy.deepcopy(signed_candidate),
            "idempotent_replay": True,
        }
    if decision is False and signed_matches:
        return {"ok": False, "error": "assessment_already_signed_off"}

    declined_matches = [
        candidate
        for candidate in matches
        if _assessment_status(candidate) in DECLINED_ASSESSMENT_STATUSES
    ]
    if declined_matches:
        canonical_declined = {
            _canonical_assessment_json(candidate)
            for candidate in declined_matches
        }
        if len(canonical_declined) != 1:
            return {"ok": False, "error": "declined_assessment_ambiguous"}
        declined_candidate = next(
            (
                candidate
                for candidate in declined_matches
                if candidate.get("durable_artifact_id")
            ),
            declined_matches[0],
        )
        fingerprint_valid, fingerprint_error, fingerprint_details = (
            validate_assessment_content_fingerprint(declined_candidate)
        )
        if not fingerprint_valid:
            return {
                "ok": False,
                "error": fingerprint_error or "assessment_content_fingerprint_invalid",
                "details": fingerprint_details,
            }
        return {
            "ok": True,
            "payload": copy.deepcopy(declined_candidate),
            "idempotent_replay": True,
        }

    matches = [
        candidate
        for candidate in matches
        if _assessment_status(candidate) in PENDING_ASSESSMENT_STATUSES
    ]
    if not matches:
        return {"ok": False, "error": "assessment_not_pending_signoff"}
    canonical = {
        _canonical_assessment_json(candidate)
        for candidate in matches
    }
    if len(canonical) != 1:
        return {"ok": False, "error": "pending_assessment_ambiguous"}
    pending = next(
        (candidate for candidate in matches if candidate.get("durable_artifact_id")),
        matches[0],
    )
    fingerprint_valid, fingerprint_error, fingerprint_details = (
        validate_assessment_content_fingerprint(pending)
    )
    if not fingerprint_valid:
        return {
            "ok": False,
            "error": fingerprint_error or "assessment_content_fingerprint_invalid",
            "details": fingerprint_details,
        }
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if decision is False:
        declined = copy.deepcopy(pending)
        declined.update(
            {
                "assessment_id": assessment_id,
                "assessment_version": version,
                "money_pool_id": money_pool_id,
                "status": "declined",
                "assessment_status": "declined",
                "signed_off": False,
                "declined_at": current_time.isoformat(),
            }
        )
        declined["signoff"] = {
            "signed_off": False,
            "declined_at": current_time.isoformat(),
            "source": "explicit_sdk_tool_decline",
        }
        return {"ok": True, "payload": declined, "idempotent_replay": False}

    status = _assessment_status(pending)
    if status not in PENDING_ASSESSMENT_STATUSES:
        return {"ok": False, "error": "assessment_not_pending_signoff"}
    assessment_content = pending.get("assessment")
    if not isinstance(assessment_content, dict) or not assessment_content:
        return {"ok": False, "error": "pending_assessment_content_missing"}
    eligible, eligibility_error, eligibility_details = validate_assessment_eligibility(
        assessment_content
    )
    if not eligible:
        return {
            "ok": False,
            "error": eligibility_error or "assessment_not_eligible",
            "details": eligibility_details,
        }
    if pending.get("requires_revalidation") is True or pending.get("stale") is True:
        return {"ok": False, "error": "pending_assessment_stale"}

    valid_until_raw = pending.get("valid_until") or pending.get("expires_at")
    if valid_until_raw in (None, ""):
        return {"ok": False, "error": "pending_assessment_valid_until_required"}
    valid_until = _parse_iso_datetime(valid_until_raw)
    if valid_until_raw not in (None, "") and valid_until is None:
        return {"ok": False, "error": "pending_assessment_valid_until_invalid"}
    if valid_until is not None and valid_until <= current_time:
        return {"ok": False, "error": "pending_assessment_expired"}

    signed = copy.deepcopy(pending)
    signed.update(
        {
            "assessment_id": assessment_id,
            "assessment_version": version,
            "money_pool_id": money_pool_id,
            "status": "signed_off",
            "assessment_status": "signed_off",
            "signed_off": True,
            "signed_off_at": current_time.isoformat(),
            "assessment": copy.deepcopy(assessment_content),
        }
    )
    # Preserve the exact validated validity timestamp that the client reviewed;
    # normalizing its textual representation here would invalidate the signed
    # content fingerprint even when the instant is equivalent.
    signed["signoff"] = {
        "signed_off": True,
        "signed_off_at": current_time.isoformat(),
        "source": "explicit_sdk_tool_approval",
    }
    return {"ok": True, "payload": signed, "idempotent_replay": False}


def _assessment_candidates(client_file: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(client_file, dict):
        return []
    rows: List[Dict[str, Any]] = []
    for key in (
        "investment_assessments",
        "assessments",
        "financial_planning_assessments",
    ):
        rows.extend(_dict_rows(client_file.get(key)))
    artifacts = client_file.get("artifacts")
    if isinstance(artifacts, dict):
        rows.extend(_dict_rows(artifacts.get("plans")))
        rows.extend(_dict_rows(artifacts.get("assessments")))
    plans = client_file.get("plans")
    if isinstance(plans, dict):
        rows.extend(_dict_rows(plans.get("writebacks")))
        rows.extend(_dict_rows(plans.get("artifacts")))
    else:
        rows.extend(_dict_rows(plans))
    recent_writebacks = client_file.get("recent_writebacks")
    if isinstance(recent_writebacks, list):
        for writeback in recent_writebacks:
            if not isinstance(writeback, dict) or writeback.get("operation") not in {
                "create_investment_assessment",
                "record_assessment_signoff",
            }:
                continue
            values = writeback.get("values")
            if isinstance(values, dict):
                rows.append(values)

    candidates: List[Dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
        if isinstance(payload, dict) and payload.get("assessment_id"):
            candidates.append(payload)
    return prefer_first_assessment_by_identity(candidates)


def _dict_rows(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        if value.get("assessment_id"):
            return [value]
        items = value.get("items")
        if isinstance(items, list):
            return [row for row in items if isinstance(row, dict)]
        return [row for row in value.values() if isinstance(row, dict)]
    return []


def _assessment_id(value: Dict[str, Any]) -> str:
    return str(value.get("assessment_id") or value.get("id") or "").strip()


def _assessment_version(value: Dict[str, Any]) -> int:
    version = value.get("assessment_version")
    if version is None:
        version = value.get("version")
    if isinstance(version, int) and not isinstance(version, bool):
        return version
    if isinstance(version, str) and version.isdigit():
        return int(version)
    return 0


def _money_pool_id(value: Dict[str, Any]) -> str:
    assessment = value.get("assessment")
    basis = assessment.get("basis") if isinstance(assessment, dict) else {}
    basis = basis if isinstance(basis, dict) else {}
    return str(
        value.get("money_pool_id")
        or basis.get("money_pool_id")
        or basis.get("pool_id")
        or ""
    ).strip()


def _assessment_status(value: Dict[str, Any]) -> str:
    return str(
        value.get("assessment_status") or value.get("status") or ""
    ).strip().lower()


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_assessment_json(value: Dict[str, Any]) -> str:
    normalized = copy.deepcopy(value)
    normalized.pop("durable_artifact_id", None)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )

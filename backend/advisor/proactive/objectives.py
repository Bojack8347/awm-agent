"""Proactive objective ranking based on Client File state.

The architecture spec describes a proactive engine that recomputes the ranked
next-best engagement after each Client File change and only fires when there is
both an objective and a writeback target. This module is that v2 boundary: it
does not talk to the user and it does not persist anything. It returns a stable
objective contract for the Main Agent to narrate or dispatch from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable, Dict, Iterable, List, Optional


def top_objective_from_client_file(client_file: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the highest ranked objective for the current Client File."""
    objectives = ranked_objectives_from_client_file(client_file)
    if objectives:
        return objectives[0]
    return None


def ranked_objectives_from_client_file(client_file: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return ranked next-best engagements with writeback targets.

    If the Client File view already provides objectives, they are normalized and
    sorted. If not, the engine derives minimal objectives from the same Client
    File spine: draft facts, onboarding progress, incomplete money pools,
    pending proposals, and stale policy/proposal impacts.
    """
    if not isinstance(client_file, dict):
        return []

    existing = [
        _normalize_objective(item, source_view="client_file")
        for item in _dicts(client_file.get("engagement_objectives"))
    ]
    derived = _derive_objectives(client_file)
    objectives = _dedupe_by_id([*existing, *derived])
    objectives = [
        objective for objective in objectives
        if objective.get("status") == "open"
        and objective.get("lifecycle_stage") in {"active", "ready", None}
        and isinstance(objective.get("target_writeback"), dict)
    ]
    return sorted(
        objectives,
        key=lambda item: (
            -float(item.get("score") or 0),
            int(item.get("priority") or 999),
            str(item.get("id") or ""),
        ),
    )


def _derive_objectives(client_file: Dict[str, Any]) -> List[Dict[str, Any]]:
    objectives: List[Dict[str, Any]] = []
    draft_facts = _dicts(client_file.get("draft_facts"))
    if draft_facts:
        objectives.append(_objective(
            objective_id="confirm_facts:draft_facts",
            objective="confirm_pending_facts",
            ask="confirm the draft facts before saving them to the Client File",
            target_writeback={
                "type": "facts",
                "record": "client_file.facts",
                "operation": "commit_facts",
                "fields": ["confirmed_facts"],
            },
            score=0.97,
            priority=5,
            generator="fact_confirmation",
            phase="confirm_facts",
            subject="Draft facts",
            retire_when="draft_facts is empty",
        ))

    onboarding = client_file.get("onboarding_progress") or client_file.get("onboarding") or {}
    if isinstance(onboarding, dict) and onboarding.get("status") not in {None, "complete", "completed"}:
        missing = onboarding.get("missing_slots") or onboarding.get("missing_fields") or []
        objectives.append(_objective(
            objective_id="onboarding:complete",
            objective="complete_onboarding",
            ask=_ask_for_missing("continue onboarding", missing),
            target_writeback={
                "type": "facts",
                "record": "client_file.facts",
                "operation": "draft_fact",
                "fields": [str(field) for field in missing if field],
            },
            score=0.72 if missing else 0.62,
            priority=20,
            generator="proactive_planner",
            phase="onboarding",
            subject="Client profile",
            retire_when="onboarding.status is complete",
        ))

    for pool in _dicts(client_file.get("money_pools")):
        missing = _missing_money_pool_fields(pool)
        if not missing:
            continue
        pool_id = str(pool.get("id") or pool.get("label") or "money_pool")
        objectives.append(_objective(
            objective_id=f"money_pool:{pool_id}:complete_inputs",
            objective="complete_investment_inputs",
            ask=_ask_for_missing("confirm investment inputs", missing),
            target_writeback={
                "type": "money_pool",
                "record": "client_file.money_pools",
                "operation": "upsert_money_pool",
                "subject_id": pool.get("id"),
                "fields": missing,
            },
            score=0.83,
            priority=15,
            generator="proactive_planner",
            phase="investment_consult",
            subject=pool.get("label") or pool_id,
            subject_id=pool.get("id"),
            retire_when="money_pool missing_fields is empty",
        ))

    for proposal in _dicts(client_file.get("proposals")):
        status = str(proposal.get("status") or "").lower()
        if status in {"approved", "deferred", "declined", "closed", "needs_revision"}:
            continue
        if proposal.get("review_outcome"):
            continue
        proposal_id = str(proposal.get("id") or proposal.get("label") or "proposal")
        objectives.append(_objective(
            objective_id=f"proposal:{proposal_id}:review",
            objective="review_proposal",
            ask="review the proposal and choose approve, refine, or defer",
            target_writeback={
                "type": "proposal_review",
                "record": "client_file.proposals",
                "operation": "record_policy_review_outcome",
                "subject_id": proposal.get("id"),
                "fields": ["decision", "rationale"],
            },
            score=0.88,
            priority=10,
            generator="proactive_planner",
            phase="policy_review",
            subject=proposal.get("label") or proposal_id,
            subject_id=proposal.get("id"),
            retire_when="proposal has review_outcome or terminal status",
        ))

    for impact in _dicts(client_file.get("stale_impacts")):
        record = str(impact.get("record") or "").lower()
        if record not in {"policy", "proposal"}:
            continue
        subject_id = impact.get("source_id") or impact.get("subject_id")
        objectives.append(_objective(
            objective_id=f"{record}:{subject_id or 'unknown'}:stale_review",
            objective=f"review_stale_{record}",
            ask=f"review whether the affected {record} still fits the updated client facts",
            target_writeback={
                "type": f"{record}_review",
                "record": f"client_file.{record}s",
                "operation": "record_review_outcome",
                "subject_id": subject_id,
                "fields": ["review_outcome", "rationale"],
            },
            score=0.86 if record == "policy" else 0.8,
            priority=8,
            generator="proactive_planner",
            phase="regular_consult",
            subject=impact.get("reason") or f"Stale {record}",
            subject_id=subject_id,
            retire_when=f"affected {record} is reviewed or refreshed",
        ))

    return objectives


def _normalize_objective(item: Dict[str, Any], *, source_view: str) -> Dict[str, Any]:
    objective_id = str(item.get("id") or item.get("objective") or "objective")
    score = item.get("score")
    if score is None:
        score = 0.7
    normalized = {
        **item,
        "id": objective_id,
        "status": item.get("status") or "open",
        "lifecycle_stage": item.get("lifecycle_stage") or "active",
        "generator": item.get("generator") or "proactive_planner",
        "source": item.get("source") or "proactive_engine",
        "source_view": item.get("source_view") or source_view,
        "score": round(float(score), 4),
    }
    target = normalized.get("target_writeback")
    if isinstance(target, dict):
        normalized["target_writeback"] = _normalize_target_writeback(target)
    return normalized


def _objective(
    *,
    objective_id: str,
    objective: str,
    ask: str,
    target_writeback: Dict[str, Any],
    score: float,
    priority: int,
    generator: str,
    phase: str,
    subject: Any,
    retire_when: str,
    subject_id: Any = None,
) -> Dict[str, Any]:
    return {
        "id": objective_id,
        "status": "open",
        "lifecycle_stage": "active",
        "generator": generator,
        "source": "proactive_engine",
        "source_view": "advisor.proactive.objectives",
        "priority": priority,
        "phase": phase,
        "objective": objective,
        "subject": subject,
        "subject_id": subject_id,
        "ask": ask,
        "target_writeback": _normalize_target_writeback(target_writeback),
        "retire_when": retire_when,
        "score": round(score, 4),
    }


def _normalize_target_writeback(target: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(target)
    record = str(out.get("record") or "")
    if not out.get("type"):
        out["type"] = _target_type_from_record(record)
    out.setdefault("operation", "update")
    fields = out.get("fields")
    out["fields"] = fields if isinstance(fields, list) else []
    return out


def _target_type_from_record(record: str) -> str:
    if "money_pool" in record:
        return "money_pool"
    if "proposal" in record:
        return "proposal_review"
    if "polic" in record:
        return "policy"
    if "fact" in record or "onboarding" in record:
        return "facts"
    return "client_file"


def _missing_money_pool_fields(pool: Dict[str, Any]) -> List[str]:
    required = ("purpose_type", "amount", "horizon_text", "risk_tolerance")
    missing: List[str] = []
    for field in required:
        value = pool.get(field)
        if field == "horizon_text":
            value = value or pool.get("horizon_date") or pool.get("horizon_years")
        if value in (None, "", [], {}):
            missing.append(field)
    return missing


def _ask_for_missing(prefix: str, missing: Any) -> str:
    fields = [str(field) for field in missing if field] if isinstance(missing, list) else []
    if not fields:
        return prefix
    if len(fields) == 1:
        return f"{prefix}: {fields[0]}"
    if len(fields) == 2:
        return f"{prefix}: {fields[0]} and {fields[1]}"
    return f"{prefix}: {', '.join(fields[:-1])}, and {fields[-1]}"


def _dicts(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dedupe_by_id(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    for item in items:
        objective_id = str(item.get("id") or "")
        if not objective_id:
            continue
        previous = by_id.get(objective_id)
        if previous is None or float(item.get("score") or 0) > float(previous.get("score") or 0):
            by_id[objective_id] = item
    return list(by_id.values())


@dataclass(frozen=True)
class RegularConsultSchedule:
    """Configuration for an outbound regular-consult delivery batch."""

    enabled: bool
    client_ids: List[str]
    date_key: str
    source: str = "env"


def regular_consult_schedule_from_env(*, env_getter: Any) -> RegularConsultSchedule:
    """Build a regular-consult schedule from environment-like config."""
    enabled = env_getter("AWM_V2_REGULAR_CONSULT_SCHEDULER", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    client_ids = [
        item.strip()
        for item in env_getter("AWM_V2_REGULAR_CONSULT_CLIENT_IDS", "").split(",")
        if item.strip()
    ]
    date_key = (
        env_getter("AWM_V2_REGULAR_CONSULT_DATE_KEY", "").strip()
        or datetime.now(timezone.utc).strftime("%Y%m%d")
    )
    return RegularConsultSchedule(enabled=enabled, client_ids=client_ids, date_key=date_key)


def run_scheduled_regular_consult_for_client(
    *,
    client_id: str,
    runtime: Any,
    date_key: str | None = None,
) -> Dict[str, Any]:
    """Run one scheduled regular-consult turn for a client."""
    stable_date_key = date_key or datetime.now(timezone.utc).strftime("%Y%m%d")
    session_id = f"scheduled-regular-consult-{client_id}-{stable_date_key}"
    return runtime.run_turn(
        client_id=client_id,
        session_id=session_id,
        user_message="[scheduled regular consult trigger]",
        turn_type="proactive_scheduled",
        channel="text",
    )


def run_scheduled_regular_consult_batch(
    *,
    client_ids: list[str],
    runtime: Any,
    date_key: str | None = None,
) -> list[Dict[str, Any]]:
    """Run scheduled regular-consult turns for many clients."""
    return [
        run_scheduled_regular_consult_for_client(client_id=client_id, runtime=runtime, date_key=date_key)
        for client_id in client_ids
    ]


class RegularConsultScheduler:
    """Replaceable boundary for outbound regular-consult delivery."""

    def __init__(self, *, runtime_factory: Any) -> None:
        self.runtime_factory = runtime_factory

    def run(self, schedule: RegularConsultSchedule) -> Dict[str, Any]:
        if not schedule.enabled:
            return {"ok": True, "status": "disabled", "delivered_count": 0, "results": []}
        if not schedule.client_ids:
            return {"ok": True, "status": "no_clients", "delivered_count": 0, "results": []}
        runtime = self.runtime_factory()
        results = run_scheduled_regular_consult_batch(
            client_ids=schedule.client_ids,
            runtime=runtime,
            date_key=schedule.date_key,
        )
        return {
            "ok": True,
            "status": "delivered",
            "delivered_count": len(results),
            "date_key": schedule.date_key,
            "client_ids": schedule.client_ids,
            "results": results,
        }


@dataclass(frozen=True)
class RegularConsultTaskConfig:
    """Runtime options for the production scheduled regular-consult worker."""

    date_key: str
    client_ids: List[str]
    limit: int = 100
    allowed_phases: tuple[str, ...] = ("regular_consult", "policy_review", "investment_consult")


def regular_consult_task_config_from_request(body: Dict[str, Any]) -> RegularConsultTaskConfig:
    """Normalize a Cloud Scheduler/internal-task payload."""
    date_key = str(body.get("date_key") or "").strip() or datetime.now(timezone.utc).strftime("%Y%m%d")
    raw_client_ids = body.get("client_ids")
    client_ids: List[str] = []
    if isinstance(raw_client_ids, list):
        client_ids = [str(item).strip() for item in raw_client_ids if str(item).strip()]
    client_id = str(body.get("client_id") or "").strip()
    if client_id and client_id not in client_ids:
        client_ids.append(client_id)
    try:
        limit = int(body.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 500))
    raw_phases = body.get("allowed_phases")
    if isinstance(raw_phases, list):
        phases = tuple(str(item).strip() for item in raw_phases if str(item).strip())
    else:
        phases = ("regular_consult", "policy_review", "investment_consult")
    return RegularConsultTaskConfig(
        date_key=date_key,
        client_ids=client_ids,
        limit=limit,
        allowed_phases=phases,
    )


class ScheduledRegularConsultTask:
    """Production worker for scheduled AWM-led returning-client check-ins.

    It enumerates eligible clients, reads each Client File, picks the top
    proactive objective, and invokes the same v2 runtime as normal chat. The
    business event stream is used as the phase-1 delivery log and in-app
    notification source.
    """

    def __init__(
        self,
        *,
        runtime_factory: Callable[[], Any],
        client_file_reader: Any,
        active_client_ids_factory: Callable[[], List[str]],
        publish_event: Callable[..., Any],
        list_events: Callable[..., List[Dict[str, Any]]],
    ) -> None:
        self.runtime_factory = runtime_factory
        self.client_file_reader = client_file_reader
        self.active_client_ids_factory = active_client_ids_factory
        self.publish_event = publish_event
        self.list_events = list_events

    def run(self, config: RegularConsultTaskConfig) -> Dict[str, Any]:
        candidate_client_ids = config.client_ids or self.active_client_ids_factory()
        candidate_client_ids = _unique_strings(candidate_client_ids)[: config.limit]
        if not candidate_client_ids:
            return {"ok": True, "status": "no_clients", "date_key": config.date_key, "results": []}

        runtime = self.runtime_factory()
        results = [
            self._run_for_client(
                client_id=client_id,
                runtime=runtime,
                date_key=config.date_key,
                allowed_phases=set(config.allowed_phases),
            )
            for client_id in candidate_client_ids
        ]
        delivered = [item for item in results if item.get("status") == "delivered"]
        skipped = [item for item in results if item.get("status") != "delivered"]
        return {
            "ok": True,
            "status": "delivered" if delivered else "no_deliveries",
            "date_key": config.date_key,
            "candidate_count": len(candidate_client_ids),
            "delivered_count": len(delivered),
            "skipped_count": len(skipped),
            "results": results,
        }

    def _run_for_client(
        self,
        *,
        client_id: str,
        runtime: Any,
        date_key: str,
        allowed_phases: set[str],
    ) -> Dict[str, Any]:
        snapshot = self.client_file_reader.read(client_id)
        client_file = snapshot.payload if hasattr(snapshot, "payload") else {}
        objective = top_objective_from_client_file(client_file)
        if not objective:
            return {"client_id": client_id, "status": "skipped", "reason": "no_objective"}
        phase = str(objective.get("phase") or "")
        if phase not in allowed_phases:
            return {
                "client_id": client_id,
                "status": "skipped",
                "reason": "phase_not_allowed",
                "objective_id": objective.get("id"),
                "phase": phase,
            }

        event_key = _regular_consult_event_key(client_id=client_id, date_key=date_key, objective=objective)
        if self._already_delivered(client_id=client_id, event_key=event_key):
            return {
                "client_id": client_id,
                "status": "skipped",
                "reason": "already_delivered",
                "objective_id": objective.get("id"),
                "event_key": event_key,
            }

        session_id = f"scheduled-regular-consult-{client_id}-{date_key}"
        result = runtime.run_turn(
            client_id=client_id,
            session_id=session_id,
            user_message="[scheduled regular consult trigger]",
            turn_type="proactive_scheduled",
            channel="text",
        )
        notification = _regular_consult_notification_payload(
            client_id=client_id,
            date_key=date_key,
            session_id=session_id,
            objective=objective,
            runtime_result=result,
        )
        event = self.publish_event(
            event_type="regular_consult.notification",
            client_id=client_id,
            aggregate_type="regular_consult_delivery",
            aggregate_id=f"{client_id}:{date_key}",
            event_source="advisor.scheduler",
            event_key=event_key,
            payload=notification,
            status="pending",
        )
        return {
            "client_id": client_id,
            "status": "delivered",
            "objective_id": objective.get("id"),
            "phase": phase,
            "session_id": session_id,
            "event_key": event_key,
            "event_id": (event or {}).get("id") if isinstance(event, dict) else None,
            "selected_skill": result.get("selected_skill") if isinstance(result, dict) else None,
        }

    def _already_delivered(self, *, client_id: str, event_key: str) -> bool:
        try:
            events = self.list_events(
                client_id=client_id,
                event_type="regular_consult.notification",
                limit=50,
            ) or []
        except Exception:
            events = []
        return any(str(event.get("event_key") or "") == event_key for event in events if isinstance(event, dict))


def _regular_consult_event_key(*, client_id: str, date_key: str, objective: Dict[str, Any]) -> str:
    raw = json.dumps(
        {
            "client_id": client_id,
            "date_key": date_key,
            "objective_id": objective.get("id"),
            "subject_id": objective.get("subject_id"),
        },
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"advisor:regular_consult:{client_id}:{date_key}:{digest}"


def _regular_consult_notification_payload(
    *,
    client_id: str,
    date_key: str,
    session_id: str,
    objective: Dict[str, Any],
    runtime_result: Dict[str, Any],
) -> Dict[str, Any]:
    subject = str(objective.get("subject") or "your plan").strip()
    ask = str(objective.get("ask") or "review your financial plan").strip()
    response_text = str((runtime_result or {}).get("response_text") or "").strip()
    body = response_text[:220] if response_text else f"AWM has a check-in ready to {ask}."
    return {
        "id": f"regular-consult-{client_id}-{date_key}",
        "notification_type": "business_card",
        "status": "unread",
        "title": "AWM has a check-in ready",
        "body": body,
        "route": "/advisor",
        "payload": {
            "client_id": client_id,
            "date_key": date_key,
            "conversation_id": session_id,
            "objective_id": objective.get("id"),
            "objective": objective.get("objective"),
            "subject": subject,
            "ask": ask,
            "selected_skill": (runtime_result or {}).get("selected_skill"),
        },
    }


def _unique_strings(values: Iterable[Any]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out

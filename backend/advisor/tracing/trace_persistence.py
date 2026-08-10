"""Best-effort persistence for AWM advisor turn traces.

The advisor runtime already returns business-readable trace events for every turn.
This module stores those events in two places:

* the existing `trace_events` table when a database is configured;
* a local JSONL file for developer debugging, even when the DB is unavailable.

Tracing must never break a user conversation, so every write is best-effort.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from api.services.tracing import safe_trace_events
from advisor.tracing.tracing import persistent_safe_tool_results


DEFAULT_TRACE_LOG = Path(".logs") / "advisor_trace.jsonl"


def persist_advisor_turn_trace(
    *,
    client_id: str,
    session_id: str,
    user_message: str,
    result: Dict[str, Any],
    turn_id: Optional[str] = None,
    channel: Optional[str] = None,
    turn_type: Optional[str] = None,
    source_type: str = "advisor",
) -> Dict[str, Any]:
    """Persist one advisor turn trace and return write stats."""

    resolved_turn_id = (
        turn_id
        or str(result.get("turn_id") or "").strip()
        or f"turn_{uuid.uuid4().hex[:12]}"
    )
    trace_id = (
        str(result.get("trace_id") or "").strip()
        or _trace_id(session_id=session_id, turn_id=resolved_turn_id)
    )
    persistent_result = {
        **result,
        "tool_results": persistent_safe_tool_results(
            result.get("tool_results")
        ),
    }
    trace_events = list(persistent_result.get("trace_events") or [])
    response_text = str(result.get("response_text") or result.get("message") or "")
    selected_skill = result.get("selected_skill")
    resolved_channel = channel or str(result.get("channel") or "text")
    resolved_turn_type = turn_type or str(result.get("turn_type") or "user_message")

    rows = _build_rows(
        trace_id=trace_id,
        turn_id=resolved_turn_id,
        client_id=client_id,
        session_id=session_id,
        user_message=user_message,
        response_text=response_text,
        selected_skill=str(selected_skill or ""),
        channel=resolved_channel,
        turn_type=resolved_turn_type,
        source_type=source_type,
        trace_events=trace_events,
        result=persistent_result,
    )
    db_written = safe_trace_events(
        {
            "trace_id": trace_id,
            "client_id": client_id,
            "session_id": session_id,
            "turn_id": resolved_turn_id,
            "source_type": source_type,
            "event_type": row["event_type"],
            "event_name": row.get("event_name"),
            "actor_type": row.get("actor_type"),
            "actor_id": row.get("actor_id"),
            "status": row.get("status", "success"),
            "agent_name": row.get("agent_name"),
            "tool_name": row.get("tool_name"),
            "engine_name": row.get("engine_name"),
            "input_summary": row.get("input_summary"),
            "output_summary": row.get("output_summary"),
            "payload": row.get("payload") if isinstance(row.get("payload"), dict) else {},
            "subjects": row.get("subjects") if isinstance(row.get("subjects"), list) else [],
        }
        for row in rows
    )

    jsonl_written = _append_jsonl(rows)
    return {
        "trace_id": trace_id,
        "turn_id": resolved_turn_id,
        "event_count": len(rows),
        "db_written": db_written,
        "jsonl_written": jsonl_written,
        "log_path": str(_trace_log_path()),
    }


def _build_rows(
    *,
    trace_id: str,
    turn_id: str,
    client_id: str,
    session_id: str,
    user_message: str,
    response_text: str,
    selected_skill: str,
    channel: str,
    turn_type: str,
    source_type: str,
    trace_events: Iterable[Dict[str, Any]],
    result: Dict[str, Any],
) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    rows: List[Dict[str, Any]] = [
        {
            "trace_id": trace_id,
            "turn_id": turn_id,
            "client_id": client_id,
            "session_id": session_id,
            "source_type": source_type,
            "event_type": "advisor.turn_started",
            "event_name": "turn_started",
            "actor_type": "client",
            "actor_id": client_id,
            "agent_name": "CompanionOrchestratorAgent",
            "input_summary": _clip(user_message or "(app entry)"),
            "output_summary": None,
            "payload": {
                "channel": channel,
                "turn_type": turn_type,
                "user_message": user_message,
            },
            "subjects": _subjects(selected_skill),
            "recorded_at": now,
        }
    ]

    for index, event in enumerate(trace_events, start=1):
        event_name = str(event.get("event") or "event")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        rows.append(
            {
                "trace_id": trace_id,
                "turn_id": turn_id,
                "client_id": client_id,
                "session_id": session_id,
                "source_type": source_type,
                "event_type": f"advisor.{event_name}",
                "event_name": event_name,
                "actor_type": "agent",
                "actor_id": "advisor_runtime",
                "agent_name": _agent_name(event_name, selected_skill),
                "tool_name": _tool_name(event_name, data),
                "engine_name": _engine_name(event_name, data),
                "input_summary": _clip(event.get("summary")),
                "output_summary": _clip(_event_output_summary(event_name, data)),
                "payload": {
                    "index": index,
                    "node": event.get("node"),
                    "summary": event.get("summary"),
                    "data": data,
                    "channel": channel,
                    "turn_type": turn_type,
                    "occurred_at": event.get("occurred_at"),
                    "trace_envelope": {
                        "trace_id": event.get("trace_id") or trace_id,
                        "turn_id": event.get("turn_id") or turn_id,
                        "span_id": event.get("span_id"),
                        "parent_span_id": event.get("parent_span_id"),
                        "sequence": event.get("sequence"),
                        "status": event.get("status"),
                        "started_at": event.get("started_at"),
                        "completed_at": event.get("completed_at"),
                        "duration_ms": event.get("duration_ms"),
                        "input": event.get("input"),
                        "output": event.get("output"),
                    },
                },
                "subjects": _subjects(selected_skill),
                "recorded_at": now,
            }
        )

    rows.append(
        {
            "trace_id": trace_id,
            "turn_id": turn_id,
            "client_id": client_id,
            "session_id": session_id,
            "source_type": source_type,
            "event_type": "advisor.turn_completed",
            "event_name": "turn_completed",
            "actor_type": "agent",
            "actor_id": "advisor_runtime",
            "agent_name": "CompanionOrchestratorAgent",
            "input_summary": _clip(user_message or "(app entry)"),
            "output_summary": _clip(response_text),
            "payload": {
                "channel": channel,
                "turn_type": turn_type,
                "selected_skill": selected_skill or None,
                "response_text": response_text,
                "planned_actions": result.get("planned_actions", []),
                "tool_results": result.get("tool_results", []),
                "subagent_artifacts": result.get("subagent_artifacts", []),
                "errors": result.get("errors", []),
                "timing": result.get("timing", {}),
                "llm_calls": result.get("llm_calls", []),
                "root_span_id": result.get("root_span_id"),
            },
            "subjects": _subjects(selected_skill),
            "recorded_at": now,
        }
    )
    return rows


def _append_jsonl(rows: Iterable[Dict[str, Any]]) -> int:
    path = _trace_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(
                    json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
                    + "\n"
                )
                count += 1
        return count
    except Exception as exc:  # pragma: no cover - tracing is best-effort
        print(f"[trace] advisor jsonl trace skipped: {exc}", flush=True)
        return 0


def _trace_log_path() -> Path:
    configured = str(os.getenv("AWM_ADVISOR_TRACE_LOG") or "").strip()
    return Path(configured) if configured else DEFAULT_TRACE_LOG


def _trace_id(*, session_id: str, turn_id: Optional[str]) -> str:
    if turn_id:
        return f"tr_advisor_{session_id}_{turn_id}"
    return f"tr_advisor_{session_id}_{uuid.uuid4().hex[:12]}"


def _subjects(selected_skill: str) -> List[Dict[str, str]]:
    if not selected_skill:
        return []
    return [{"subject_type": "skill", "subject_id": selected_skill, "relation": "selected"}]


def _agent_name(event_name: str, selected_skill: str) -> str:
    if event_name == "actions_executed":
        return "CompanionOrchestratorAgent+SilentAgents"
    if selected_skill:
        return f"CompanionOrchestratorAgent:{selected_skill}"
    return "CompanionOrchestratorAgent"


def _tool_name(event_name: str, data: Dict[str, Any]) -> Optional[str]:
    if event_name != "actions_executed":
        return None
    tools: List[str] = []
    for item in data.get("tool_results") or []:
        if isinstance(item, dict) and item.get("tool"):
            tools.append(str(item["tool"]))
    return ",".join(tools[:5]) or None


def _engine_name(event_name: str, data: Dict[str, Any]) -> Optional[str]:
    if event_name != "actions_executed":
        return None
    engines: List[str] = []
    for item in data.get("subagent_artifacts") or []:
        if isinstance(item, dict) and item.get("subagent"):
            engines.append(str(item["subagent"]))
    return ",".join(engines[:5]) or None


def _event_output_summary(event_name: str, data: Dict[str, Any]) -> str:
    if event_name == "skill_selected":
        return str(data.get("selected_skill") or "")
    if event_name == "action_planned":
        actions = data.get("planned_actions") or []
        return f"{len(actions)} action(s) planned"
    if event_name == "actions_executed":
        return (
            f"{data.get('tool_result_count', 0)} tool result(s), "
            f"{data.get('subagent_artifact_count', 0)} sub-agent artifact(s)"
        )
    return ""


def _clip(value: Any, limit: int = 500) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


__all__ = ["persist_advisor_turn_trace"]

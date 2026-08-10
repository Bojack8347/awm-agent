"""Business-readable trace events for AWM agent v2 turns."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class TurnTraceEvent(BaseModel):
    """One business-level event emitted during a companion turn.

    LangGraph/LangSmith can show low-level execution details. This schema is
    AWM-specific: it explains what the advisor decided in product language.
    """

    model_config = ConfigDict(extra="forbid")

    event: str
    node: str
    summary: str
    trace_id: Optional[str] = None
    turn_id: Optional[str] = None
    span_id: Optional[str] = None
    parent_span_id: Optional[str] = None
    sequence: Optional[int] = None
    status: str = "success"
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_ms: Optional[float] = None
    input: Optional[Dict[str, Any]] = None
    output: Optional[Dict[str, Any]] = None
    data: Dict[str, Any] = Field(default_factory=dict)
    occurred_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def trace_event(
    *,
    event: str,
    node: str,
    summary: str,
    data: Optional[Dict[str, Any]] = None,
    trace_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    span_id: Optional[str] = None,
    parent_span_id: Optional[str] = None,
    sequence: Optional[int] = None,
    status: str = "success",
    started_at: Optional[str] = None,
    completed_at: Optional[str] = None,
    duration_ms: Optional[float] = None,
    input_data: Any = None,
    output_data: Any = None,
) -> Dict[str, Any]:
    """Build a JSON-serializable trace event."""

    resolved_completed_at = completed_at or utc_now_iso()
    return TurnTraceEvent(
        event=event,
        node=node,
        summary=summary,
        trace_id=trace_id,
        turn_id=turn_id,
        span_id=span_id or new_span_id(node),
        parent_span_id=parent_span_id,
        sequence=sequence,
        status=status,
        started_at=started_at,
        completed_at=resolved_completed_at,
        duration_ms=duration_ms,
        input=capture_trace_io(input_data) if input_data is not None else None,
        output=capture_trace_io(output_data) if output_data is not None else None,
        data=data or {},
        occurred_at=resolved_completed_at,
    ).model_dump(exclude_none=True)


def append_trace(
    existing: Iterable[Dict[str, Any]],
    *,
    event: str,
    node: str,
    summary: str,
    data: Optional[Dict[str, Any]] = None,
    trace_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    span_id: Optional[str] = None,
    parent_span_id: Optional[str] = None,
    status: str = "success",
    started_at: Optional[str] = None,
    completed_at: Optional[str] = None,
    duration_ms: Optional[float] = None,
    input_data: Any = None,
    output_data: Any = None,
) -> List[Dict[str, Any]]:
    """Return a new trace list with one event appended."""

    items = list(existing or [])
    previous = items[-1] if items else {}
    items.append(
        trace_event(
            event=event,
            node=node,
            summary=summary,
            data=data,
            trace_id=trace_id or previous.get("trace_id"),
            turn_id=turn_id or previous.get("turn_id"),
            span_id=span_id,
            parent_span_id=parent_span_id,
            sequence=len(items) + 1,
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            input_data=input_data,
            output_data=output_data,
        )
    )
    return items


def utc_now_iso() -> str:
    """Return a stable UTC timestamp for trace envelopes."""

    return datetime.now(timezone.utc).isoformat()


def new_span_id(label: str = "span") -> str:
    """Create an opaque span id; the node/name carries the readable label."""

    del label
    return f"sp_{uuid.uuid4().hex[:16]}"


def trace_full_io_enabled() -> bool:
    """Whether local acceptance evidence should retain redacted full values."""

    return str(os.getenv("AWM_AI_V2_TRACE_CAPTURE_FULL_IO") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def capture_trace_io(value: Any, *, full: Optional[bool] = None) -> Dict[str, Any]:
    """Capture auditable I/O without exposing secrets by default.

    Production API responses retain a digest and byte count. Local acceptance
    runs can enable AWM_AI_V2_TRACE_CAPTURE_FULL_IO to retain the redacted
    value in DB/JSONL evidence. Secret-like keys are always redacted.
    """

    sanitized = _redact_trace_value(_jsonable(value))
    serialized = json.dumps(
        sanitized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    capture_full = trace_full_io_enabled() if full is None else bool(full)
    result: Dict[str, Any] = {
        "capture_mode": "full_redacted" if capture_full else "digest",
        "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "bytes": len(serialized.encode("utf-8")),
        "value_type": type(value).__name__,
    }
    if capture_full:
        result["value"] = sanitized
    return result


def persistent_safe_tool_result(result: Any) -> Any:
    """Remove ephemeral public-fact authorization data from durable diagnostics."""

    if not isinstance(result, dict):
        return result
    if result.get("tool") != "research_public_financial_fact":
        return _redact_session_authorization_tokens(result)

    safe = {
        key: result.get(key)
        for key in (
            "tool",
            "tool_call_id",
            "ok",
            "read_only",
            "writeback_target",
            "retry_allowed",
            "error",
            "research_attempted",
            "executed_by_agent",
            "arguments",
            "note",
        )
        if key in result
    }
    full_result = (
        result.get("full_result")
        if isinstance(result.get("full_result"), dict)
        else {}
    )
    if full_result:
        authorization = (
            full_result.get("authorization")
            if isinstance(full_result.get("authorization"), dict)
            else {}
        )
        fact = (
            full_result.get("fact")
            if isinstance(full_result.get("fact"), dict)
            else {}
        )
        safe["full_result"] = {
            "schema_version": full_result.get("schema_version"),
            "authorization": {
                key: authorization.get(key)
                for key in (
                    "scope",
                    "expires_at",
                    "human_review_required",
                    "durable",
                    "reporting_allowed",
                    "session_calculation_allowed",
                    "durable_model_input_allowed",
                    "recommendation_allowed",
                )
                if key in authorization
            },
            "fact_metadata": {
                key: fact.get(key)
                for key in (
                    "variable_key",
                    "effective_year",
                    "unit",
                    "jurisdiction",
                    "content_sha256",
                    "retrieved_at",
                    "origin",
                )
                if key in fact
            },
            "sources": full_result.get("sources") or [],
            "durable_promotion": full_result.get("durable_promotion"),
            "disclosure": full_result.get("disclosure"),
        }
    evidence = (
        result.get("recommendation_evidence")
        if isinstance(result.get("recommendation_evidence"), dict)
        else {}
    )
    if evidence:
        safe["recommendation_evidence"] = {
            key: evidence.get(key)
            for key in (
                "schema_version",
                "tool",
                "status",
                "execution_ok",
                "valid_for_reporting",
                "valid_for_conclusion",
                "valid_for_recommendation",
                "permitted_use",
                "warnings",
                "errors",
            )
            if key in evidence
        }
    return _redact_session_authorization_tokens(safe)


def persistent_safe_tool_results(results: Any) -> List[Any]:
    if not isinstance(results, list):
        return []
    return [persistent_safe_tool_result(item) for item in results]


_SESSION_PUBLIC_FACT_ID_RE = re.compile(
    r"session-public-fact:[a-f0-9]{32}"
)


def _redact_session_authorization_tokens(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact_session_authorization_tokens(item)
            for key, item in value.items()
            if str(key) not in {"session_fact_id", "session_scope_sha256"}
        }
    if isinstance(value, list):
        return [_redact_session_authorization_tokens(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_session_authorization_tokens(item) for item in value)
    if isinstance(value, str):
        return _SESSION_PUBLIC_FACT_ID_RE.sub(
            "[SESSION_PUBLIC_FACT_ID_REDACTED]",
            value,
        )
    return value


def build_operation_span(
    *,
    trace_id: str,
    turn_id: str,
    parent_span_id: str,
    name: str,
    kind: str,
    started_at: str,
    duration_ms: float,
    input_data: Any,
    output_data: Any = None,
    status: str = "success",
    error: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a nested tool, sub-agent, engine, DB, or service span."""

    return {
        "trace_id": trace_id,
        "turn_id": turn_id,
        "span_id": new_span_id(name),
        "parent_span_id": parent_span_id,
        "name": name,
        "kind": kind,
        "status": status,
        "started_at": started_at,
        "completed_at": utc_now_iso(),
        "duration_ms": round(float(duration_ms), 3),
        "input": capture_trace_io(input_data),
        "output": capture_trace_io(output_data) if output_data is not None else None,
        "error": error,
        "metadata": metadata or {},
    }


def build_llm_call_trace(
    *,
    trace_id: str,
    turn_id: str,
    parent_span_id: str,
    stage_name: str,
    purpose: str,
    provider: str,
    model: str,
    started_at: str,
    request_payload: Dict[str, Any],
    response_payload: Dict[str, Any],
    result: Any,
    timing: Dict[str, Any],
    status: str = "success",
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Normalize one LLM request/response into reviewable trace evidence."""

    raw = getattr(result, "raw", None) if result is not None else None
    response_id = getattr(result, "response_id", None) if result is not None else None
    provider_request_id = (
        getattr(raw, "_request_id", None)
        or getattr(raw, "request_id", None)
        or response_id
    )
    normalized_timing = {
        "request_prepare_ms": timing.get("request_prepare_ms"),
        "provider_elapsed_ms": timing.get("provider_elapsed_ms"),
        "time_to_first_token_ms": timing.get("time_to_first_token_ms"),
        "response_parse_ms": timing.get("response_parse_ms"),
        "validation_ms": timing.get("validation_ms"),
        "retry_backoff_ms": timing.get("retry_backoff_ms"),
        "total_elapsed_ms": timing.get("total_elapsed_ms"),
        "streaming": bool(timing.get("streaming")),
        "ttft_measurement": (
            "measured"
            if timing.get("time_to_first_token_ms") is not None
            else "unavailable_for_non_streaming_call"
        ),
    }
    return {
        "trace_id": trace_id,
        "turn_id": turn_id,
        "span_id": new_span_id(f"llm_{stage_name}"),
        "parent_span_id": parent_span_id,
        "kind": "llm",
        "stage_name": stage_name,
        "purpose": purpose,
        "status": status,
        "provider": provider,
        "model": model,
        "response_id": response_id,
        "provider_request_id": provider_request_id,
        "started_at": started_at,
        "completed_at": utc_now_iso(),
        "timing": normalized_timing,
        "usage": _extract_llm_usage(raw),
        "request": capture_trace_io(request_payload),
        "response": capture_trace_io(response_payload),
        "error": error,
    }


def llm_calls_from_trace(
    trace_events: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Collect nested LLM calls from business events in execution order."""

    calls: List[Dict[str, Any]] = []
    for event in trace_events:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        call = data.get("llm_call")
        if isinstance(call, dict):
            calls.append(call)
        metadata = (
            data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        )
        call = metadata.get("llm_call")
        if isinstance(call, dict) and call not in calls:
            calls.append(call)
    return calls


def trace_state_snapshot(state: Any) -> Dict[str, Any]:
    """Return the complete relevant Agent state without recursively tracing traces."""

    if hasattr(state, "model_dump") and callable(state.model_dump):
        raw = state.model_dump()
    elif isinstance(state, dict):
        raw = dict(state)
    else:
        raw = {"value": str(state)}
    trace_events = raw.pop("trace_events", [])
    raw["trace_event_count_before_snapshot"] = (
        len(trace_events) if isinstance(trace_events, list) else 0
    )
    return raw


def instrument_graph_node(
    *,
    state: Any,
    node: str,
    operation: Callable[[Any], Any],
) -> Any:
    """Attach a node-level input/output span to AWM's existing business event.

    The workflow retains its stable six business events; this enriches the event
    emitted by each node rather than adding noisy framework-only events.
    """

    started_at = utc_now_iso()
    started = time.perf_counter()
    span_id = new_span_id(node)
    working_state = (
        state.model_copy(update={"current_span_id": span_id})
        if hasattr(state, "model_copy")
        else state
    )
    input_state = trace_state_snapshot(working_state)
    result = operation(working_state)
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    if not hasattr(result, "trace_events") or not hasattr(result, "model_copy"):
        return result

    events = list(result.trace_events or [])
    target_index = next(
        (
            index
            for index in range(len(events) - 1, -1, -1)
            if str(events[index].get("node") or "") == node
        ),
        None,
    )
    if target_index is None:
        return result

    event = dict(events[target_index])
    data = dict(event.get("data") or {})
    data.setdefault("trace_schema_version", "awm.advisor.trace.v1")
    data["node_span"] = {
        "kind": "agent_node",
        "name": node,
        "duration_ms": duration_ms,
        "input_state_fields": sorted(input_state.keys()),
    }
    event.update(
        {
            "trace_id": getattr(result, "trace_id", "")
            or getattr(working_state, "trace_id", ""),
            "turn_id": getattr(result, "turn_id", "")
            or getattr(working_state, "turn_id", ""),
            "span_id": span_id,
            "parent_span_id": getattr(working_state, "root_span_id", "") or None,
            "status": "success",
            "started_at": started_at,
            "completed_at": utc_now_iso(),
            "duration_ms": duration_ms,
            "input": capture_trace_io(input_state),
            "output": capture_trace_io(trace_state_snapshot(result)),
            "data": data,
        }
    )
    events[target_index] = event
    return result.model_copy(update={"trace_events": events})


def _extract_llm_usage(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    usage = getattr(raw, "usage", None)
    normalized = _jsonable(usage)
    return normalized if isinstance(normalized, dict) else {}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _jsonable(model_dump())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return {
            str(key): _jsonable(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return str(value)


def _redact_trace_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            sensitive = (
                normalized in {
                    "authorization",
                    "password",
                    "secret",
                    "api_key",
                    "access_token",
                    "refresh_token",
                    "cookie",
                    "set_cookie",
                    "session_fact_id",
                    "session_scope_sha256",
                }
                or normalized.endswith("_api_key")
                or normalized.endswith("_secret")
                or normalized.endswith("_password")
            )
            redacted[str(key)] = "[REDACTED]" if sensitive else _redact_trace_value(item)
        return redacted
    if isinstance(value, list):
        return [_redact_trace_value(item) for item in value]
    if isinstance(value, str):
        return _SESSION_PUBLIC_FACT_ID_RE.sub(
            "[SESSION_PUBLIC_FACT_ID_REDACTED]",
            value,
        )
    return value


def format_trace_timeline(
    *,
    user_message: str,
    response_text: str,
    trace_events: Iterable[Dict[str, Any]],
) -> str:
    """Render trace events as a human-readable debugging timeline."""

    display_user = user_message if user_message.strip() else "(session opened; AWM starts proactively)"
    lines = [
        "AWM Agent V2 Turn Timeline",
        f"USER: {display_user}",
        "",
    ]
    for index, event in enumerate(trace_events, start=1):
        event_name = str(event.get("event") or "event")
        node = str(event.get("node") or "unknown_node")
        summary = str(event.get("summary") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        lines.append(f"{index}. {event_name} [{node}]")
        if summary:
            lines.append(f"   Next: {summary}")
        duration_ms = event.get("duration_ms")
        if isinstance(duration_ms, (int, float)):
            lines.append(f"   Timing: {duration_ms:.3f} ms")
        llm_call = data.get("llm_call")
        if not isinstance(llm_call, dict):
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            llm_call = metadata.get("llm_call")
        if isinstance(llm_call, dict):
            llm_timing = llm_call.get("timing") if isinstance(llm_call.get("timing"), dict) else {}
            provider_ms = llm_timing.get("provider_elapsed_ms")
            timing_text = f"; provider={provider_ms} ms" if provider_ms is not None else ""
            lines.append(
                f"   LLM: {llm_call.get('stage_name') or 'unknown'} "
                f"({llm_call.get('provider') or 'unknown'}/{llm_call.get('model') or 'unknown'})"
                f"{timing_text}"
            )
        reason = data.get("reason")
        if reason:
            lines.append(f"   Why: {reason}")
        readable = _readable_event_detail(event_name, data)
        if readable:
            lines.append(f"   Detail: {readable}")
        if data:
            lines.append(f"   Data: {_compact_json(data)}")
        lines.append("")
    lines.append(f"ASSISTANT: {response_text}")
    return "\n".join(lines).rstrip()


def _readable_event_detail(event_name: str, data: Dict[str, Any]) -> str:
    if event_name == "client_file_loaded":
        return (
            f"{data.get('open_loop_count', 0)} open loops, "
            f"{data.get('money_pool_count', 0)} money pools, "
            f"{data.get('active_policy_count', 0)} active policies, "
            f"{data.get('proposed_policy_count', 0)} proposed policies."
        )
    if event_name == "skill_selected":
        route_source = data.get("route_source") or "unknown"
        confidence = data.get("confidence")
        intent = data.get("intent_label")
        gate_result = data.get("gate_result") if isinstance(data.get("gate_result"), dict) else {}
        candidates = data.get("candidates") or []
        candidate_text = f"; candidates: {', '.join(candidates)}" if candidates else ""
        confidence_text = f"; confidence: {confidence}" if confidence is not None else ""
        intent_text = f"; intent: {intent}" if intent else ""
        gate_text = ""
        if gate_result:
            gate_text = f"; gate: {gate_result.get('reason')}"
        return (
            f"Use skill `{data.get('selected_skill')}` via {route_source}"
            f"{confidence_text}{intent_text}{candidate_text}{gate_text}; tools: "
            f"{', '.join(data.get('allowed_tools') or []) or 'none'}."
        )
    if event_name == "objective_loaded":
        objective = data.get("objective")
        if objective:
            status = data.get("status")
            stage = data.get("lifecycle_stage")
            state_text = f"; status={status}" if status else ""
            stage_text = f"; stage={stage}" if stage else ""
            return f"Top objective is `{objective}`{state_text}{stage_text}."
        return "No active proactive objective."
    if event_name == "action_planned":
        actions = data.get("planned_actions") or []
        if actions:
            return f"Planned action(s): {_summarize_actions(actions)}."
    if event_name == "actions_executed":
        service_results = data.get("service_results") or []
        blocked = [
            str(result.get("service"))
            for result in service_results
            if isinstance(result, dict) and not result.get("allowed")
        ]
        blocked_text = f"; blocked services: {', '.join(blocked)}" if blocked else ""
        writeback_text = _summarize_writebacks(data)
        if writeback_text:
            writeback_text = f"; writebacks: {writeback_text}"
        return (
            f"{data.get('tool_result_count', 0)} tool result(s), "
            f"{data.get('subagent_artifact_count', 0)} sub-agent artifact(s)"
            f"{blocked_text}{writeback_text}."
        )
    if event_name == "response_composed":
        return "Prepared the user-facing response."
    return ""


def _summarize_actions(actions: Iterable[Any]) -> str:
    parts: List[str] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("type") or "unknown")
        if action_type == "tool":
            parts.append(f"tool:{action.get('tool')}")
        elif action_type == "dispatch_subagent":
            parts.append(f"subagent:{action.get('subagent')}")
        elif action_type == "deterministic_service":
            allowed = "allowed" if action.get("allowed") else "blocked"
            parts.append(f"service:{action.get('service')}:{allowed}")
        else:
            parts.append(action_type)
    return ", ".join(parts) or "none"


def _summarize_writebacks(data: Dict[str, Any]) -> str:
    parts: List[str] = []
    for result in data.get("tool_results") or []:
        if not isinstance(result, dict):
            continue
        write_result = result.get("write_result")
        if not isinstance(write_result, dict):
            continue
        writeback = write_result.get("writeback") if isinstance(write_result.get("writeback"), dict) else {}
        record = writeback.get("record") or result.get("writeback_target")
        operation = writeback.get("operation") or result.get("tool")
        if record or operation:
            parts.append(f"{record or 'client_file'}.{operation or 'write'}")
    for artifact in data.get("subagent_artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else {}
        write_result = payload.get("write_result") if isinstance(payload.get("write_result"), dict) else {}
        writeback = write_result.get("writeback") if isinstance(write_result.get("writeback"), dict) else {}
        record = writeback.get("record") or artifact.get("writeback_target")
        artifact_type = artifact.get("artifact_type")
        if record or artifact_type:
            parts.append(f"{record or 'client_file'}.{artifact_type or 'artifact'}")
    return ", ".join(parts[:6])


def _compact_json(value: Dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

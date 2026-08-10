from __future__ import annotations

import json
from typing import Any, Dict, List

from agents.run_state import RunState

from advisor.agents.context import AwmAgentContext


def _serialize_interruptions(interruptions: List[Any]) -> List[Dict[str, Any]]:
    serialized: List[Dict[str, Any]] = []
    for index, item in enumerate(interruptions):
        raw = item.model_dump() if hasattr(item, "model_dump") else {}
        serialized.append(
            {
                "index": index,
                "type": type(item).__name__,
                "tool_name": raw.get("tool_name") or raw.get("name"),
                "raw": raw or {"repr": repr(item)},
            }
        )
    return serialized


def _apply_approval_decisions(state: RunState, approval_decisions: List[Dict[str, Any]]) -> None:
    interruptions = state.get_interruptions()
    for decision in approval_decisions:
        index = int(decision.get("index", 0))
        if index < 0 or index >= len(interruptions):
            continue
        action = str(decision.get("action") or "").lower()
        if action == "approve":
            state.approve(interruptions[index], always_approve=bool(decision.get("always")))
        elif action == "reject":
            state.reject(
                interruptions[index],
                always_reject=bool(decision.get("always")),
                rejection_message=decision.get("message"),
            )


def _json_safe_interruption_value(value: Any) -> Any:
    """Detach resumable state from live executors, sessions, locks, and callbacks."""

    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _serialize_arc_interruption_context(raw_context: Any) -> Dict[str, Any]:
    """Serialize only conversation state required after an SDK approval pause."""

    if not isinstance(raw_context, AwmAgentContext):
        return {}
    return {
        "schema_version": "awm.agent_interruption_context.v1",
        "active_skills": dict(raw_context.active_skills),
        "artifact_context": _json_safe_interruption_value(
            raw_context.artifact_context
        ),
        "trusted_action_context": _json_safe_interruption_value(
            raw_context.trusted_action_context
        ),
        "allowed_tools": (
            sorted(raw_context.allowed_tools)
            if raw_context.allowed_tools is not None
            else None
        ),
        "tool_results": _json_safe_interruption_value(raw_context.tool_results),
        "trace_events": _json_safe_interruption_value(raw_context.trace_events),
        "llm_calls": _json_safe_interruption_value(raw_context.llm_calls),
        "mid_turn_commit_refresh_verified": (
            raw_context.mid_turn_commit_refresh_verified
        ),
        "mid_turn_commit_refresh_reason": raw_context.mid_turn_commit_refresh_reason,
    }


def _restore_arc_interruption_context(
    context: AwmAgentContext,
    interrupted_state: Dict[str, Any],
) -> None:
    """Restore safe per-turn fields while retaining the fresh live executor."""

    state_context = interrupted_state.get("context")
    if not isinstance(state_context, dict):
        return
    payload = state_context.get("context")
    if not isinstance(payload, dict):
        return
    active_skills = payload.get("active_skills")
    if isinstance(active_skills, dict):
        context.active_skills.update(
            {
                str(agent_key): str(skill_name)
                for agent_key, skill_name in active_skills.items()
                if str(agent_key).strip() and str(skill_name).strip()
            }
        )
    artifact_context = payload.get("artifact_context")
    if isinstance(artifact_context, dict):
        context.artifact_context = artifact_context
    trusted_action_context = payload.get("trusted_action_context")
    if isinstance(trusted_action_context, dict):
        context.trusted_action_context = trusted_action_context
    if context.allowed_tools is None and isinstance(payload.get("allowed_tools"), list):
        context.allowed_tools = {
            str(tool_name)
            for tool_name in payload["allowed_tools"]
            if str(tool_name).strip()
        }
    for field_name in ("tool_results", "trace_events", "llm_calls"):
        values = payload.get(field_name)
        if isinstance(values, list):
            setattr(
                context,
                field_name,
                [item for item in values if isinstance(item, dict)],
            )
    if isinstance(payload.get("mid_turn_commit_refresh_verified"), bool):
        context.mid_turn_commit_refresh_verified = payload[
            "mid_turn_commit_refresh_verified"
        ]
    refresh_reason = payload.get("mid_turn_commit_refresh_reason")
    if isinstance(refresh_reason, str):
        context.mid_turn_commit_refresh_reason = refresh_reason

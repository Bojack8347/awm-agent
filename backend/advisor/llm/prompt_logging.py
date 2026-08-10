"""Shared prompt-debug logging helpers for LLM stages.

These helpers keep the pure-LLM agents aligned with ToolLoopRunner's existing
NDJSON prompt logging so local validation can measure stage timings across the
full backend, not just tool-loop stages.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _prompt_log_enabled() -> bool:
    return os.getenv("ADVISOR_TEMP_LOG_PROMPTS", "false").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _prompt_log_path() -> Path:
    backend_root = Path(__file__).resolve().parents[2]
    return Path(
        os.getenv(
            "ADVISOR_TEMP_PROMPT_LOG_PATH",
            str(backend_root / "api" / "logs" / "llm_prompt_debug.ndjson"),
        )
    )


def _safe_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_safe_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_safe_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _safe_jsonable(v) for k, v in value.items()}
    if hasattr(value, "__dict__"):
        return _safe_jsonable(vars(value))
    return str(value)


def serialize_messages(messages: List[Any]) -> List[Dict[str, Any]]:
    """Convert normalized LLM messages into JSON-safe dicts for debug logs."""
    serialized: List[Dict[str, Any]] = []
    for message in messages:
        row: Dict[str, Any] = {
            "role": str(getattr(message, "role", "") or ""),
            "content": str(getattr(message, "content", "") or ""),
        }
        name = getattr(message, "name", None)
        if name:
            row["name"] = str(name)
        tool_call_id = getattr(message, "tool_call_id", None)
        if tool_call_id:
            row["tool_call_id"] = str(tool_call_id)
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            row["tool_calls"] = [
                {
                    "id": getattr(call, "id", None),
                    "name": getattr(call, "name", None),
                    "arguments": _safe_jsonable(getattr(call, "arguments", None)),
                }
                for call in tool_calls
            ]
        serialized.append(row)
    return serialized


def append_prompt_log(
    *,
    stage: str,
    provider: str,
    model: str,
    system_instruction: str,
    temperature: Optional[float],
    contents: List[Dict[str, Any]],
    use_tools: bool = False,
    elapsed_seconds: Optional[float] = None,
    success: Optional[bool] = None,
    error: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
    journey_id: Optional[str] = None,
) -> None:
    """Append one LLM request envelope as NDJSON; never raise to caller.

    `request_id` and `journey_id` are first-class so a single user turn can
    be greppable across stages. They are populated by callers that have an
    `AgentRunContext` in scope — older call sites that don't pass them
    simply omit the field, which keeps Phase 1 backward-compatible.
    """
    if not _prompt_log_enabled():
        return

    payload = {
        "stage": stage,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "model": model,
        "system_instruction": system_instruction,
        "use_tools": use_tools,
        "temperature": temperature,
        "contents": contents,
    }
    if request_id:
        payload["request_id"] = str(request_id)
    if journey_id:
        payload["journey_id"] = str(journey_id)
    if elapsed_seconds is not None:
        payload["elapsed_seconds"] = round(float(elapsed_seconds), 3)
    if success is not None:
        payload["success"] = bool(success)
    if error:
        payload["error"] = str(error)
    if extra:
        payload.update(_safe_jsonable(extra))

    path = _prompt_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True, default=str) + "\n")
    except OSError:
        pass

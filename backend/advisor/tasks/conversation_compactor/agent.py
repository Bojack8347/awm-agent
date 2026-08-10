"""Structured, tool-free conversation compaction task."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from advisor.instructions import task_prompt_dir
from advisor.llm.adapter import (
    LLMClientFactory,
    LLMGenerateRequest,
    LLMGenerateResult,
    LLMMessage,
)
from advisor.llm.prompt_logging import append_prompt_log, serialize_messages
from advisor.runtime.task_definition import get_task_profile


_SOURCE_ITEM_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "item": {"type": "string"},
        "status": {
            "type": "string",
            "enum": ["accepted", "rejected", "revised", "deferred"],
        },
        "source_message_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["item", "status", "source_message_ids"],
}

CONVERSATION_SUMMARY_SCHEMA: Dict[str, Any] = {
    "title": "awm_conversation_summary",
    "type": "object",
    "properties": {
        "narrative": {"type": "string"},
        "consultation_stage": {
            "type": "string",
            "enum": [
                "onboarding",
                "planning",
                "assessment",
                "proposal",
                "monitoring",
                "open",
            ],
        },
        "decisions": {"type": "array", "items": _SOURCE_ITEM_SCHEMA},
        "advisor_commitments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "commitment": {"type": "string"},
                    "status": {"type": "string", "enum": ["open", "fulfilled"]},
                    "source_message_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["commitment", "status", "source_message_ids"],
            },
        },
        "open_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "source_message_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["question", "source_message_ids"],
            },
        },
        "corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "correction": {"type": "string"},
                    "source_message_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["correction", "source_message_ids"],
            },
        },
        "communication_preferences": {"type": "array", "items": {"type": "string"}},
        "unconfirmed_mentions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "statement": {"type": "string"},
                    "status": {"type": "string", "enum": ["unconfirmed"]},
                    "source_message_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["statement", "status", "source_message_ids"],
            },
        },
    },
    "required": [
        "narrative",
        "consultation_stage",
        "decisions",
        "advisor_commitments",
        "open_questions",
        "corrections",
        "communication_preferences",
        "unconfirmed_mentions",
    ],
}

_FINANCIAL_VALUE_RE = re.compile(
    r"(?:\d|[$€£]\s*\d|\b(?:interest|return|growth|inflation|discount)\s+rate\b)",
    re.IGNORECASE,
)
_AUTHORITY_CLAIM_RE = re.compile(
    r"\b(?:tool\s+(?:ran|succeeded|completed)|analysis\s+(?:ran|succeeded|completed)|(?:assessment|proposal|policy)\s+(?:was\s+)?(?:signed|approved|accepted|confirmed)|client\s+(?:approved|authorized|consented|signed\s+off)|execution\s+(?:was\s+)?(?:approved|authorized|confirmed))\b",
    re.IGNORECASE,
)


def _coverage_from_raw_messages(inputs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not inputs:
        raise ValueError("tier-1 compaction requires at least one message")
    first = inputs[0]
    last = inputs[-1]
    first_id = str(first.get("id") or "").strip()
    last_id = str(last.get("id") or "").strip()
    if not first_id or not last_id:
        raise ValueError("compaction inputs require message ids")
    first_date = str(first.get("created_at") or "")[:10]
    last_date = str(last.get("created_at") or "")[:10]
    return {
        "from_message_id": first_id,
        "through_message_id": last_id,
        "from_date": first_date,
        "through_date": last_date,
        "message_count": len(inputs),
    }


def _coverage_from_summaries(inputs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not inputs:
        raise ValueError("tier-2 compaction requires at least one summary")
    coverages = [item.get("coverage") for item in inputs]
    if not all(isinstance(item, dict) for item in coverages):
        raise ValueError("tier-2 inputs require coverage")
    first = coverages[0]
    last = coverages[-1]
    return {
        "from_message_id": str(first.get("from_message_id") or ""),
        "through_message_id": str(last.get("through_message_id") or ""),
        "from_date": str(first.get("from_date") or ""),
        "through_date": str(last.get("through_date") or ""),
        "message_count": sum(int(item.get("message_count") or 0) for item in coverages),
    }


def _source_ids(
    value: Any,
    *,
    allowed_source_message_ids: Optional[set[str]] = None,
) -> List[str]:
    if not isinstance(value, list):
        return []
    result = list(
        dict.fromkeys(str(item).strip() for item in value if str(item).strip())
    )
    if (
        allowed_source_message_ids is not None
        and not set(result) <= allowed_source_message_ids
    ):
        return []
    return result


def _source_message_ids_from_inputs(
    inputs: Sequence[Dict[str, Any]], tier: int
) -> set[str]:
    if tier == 1:
        return {
            str(item.get("id") or "").strip()
            for item in inputs
            if str(item.get("id") or "").strip()
        }

    result: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key == "source_message_ids" and isinstance(nested, list):
                    result.update(
                        str(item).strip() for item in nested if str(item).strip()
                    )
                else:
                    collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    collect(list(inputs))
    return result


def _safe_summary_text(value: Any, *, allow_financial_values: bool = False) -> str:
    text = str(value or "").strip()
    if not text or _AUTHORITY_CLAIM_RE.search(text):
        return ""
    if not allow_financial_values and _FINANCIAL_VALUE_RE.search(text):
        return ""
    return text


def validate_conversation_summary(
    payload: Dict[str, Any],
    *,
    tier: int,
    coverage: Dict[str, Any],
    allowed_source_message_ids: Optional[set[str]] = None,
) -> Dict[str, Any]:
    """Apply authority and provenance rules after structured parsing."""
    if not isinstance(payload, dict):
        raise ValueError("conversation summary must be an object")

    stage = str(payload.get("consultation_stage") or "open").strip().lower()
    if stage not in {
        "onboarding",
        "planning",
        "assessment",
        "proposal",
        "monitoring",
        "open",
    }:
        stage = "open"

    def _entries(
        key: str, text_key: str, statuses: Optional[set[str]] = None
    ) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        values = payload.get(key)
        if not isinstance(values, list):
            return result
        for value in values:
            if not isinstance(value, dict):
                continue
            text = _safe_summary_text(value.get(text_key))
            ids = _source_ids(
                value.get("source_message_ids"),
                allowed_source_message_ids=allowed_source_message_ids,
            )
            status = str(value.get("status") or "").strip().lower()
            if not text or not ids or (statuses is not None and status not in statuses):
                continue
            item: Dict[str, Any] = {text_key: text}
            if statuses is not None:
                item["status"] = status
            item["source_message_ids"] = ids
            result.append(item)
        return result

    mentions: List[Dict[str, Any]] = []
    for value in payload.get("unconfirmed_mentions") or []:
        if not isinstance(value, dict):
            continue
        statement = _safe_summary_text(
            value.get("statement"),
            allow_financial_values=True,
        )
        ids = _source_ids(
            value.get("source_message_ids"),
            allowed_source_message_ids=allowed_source_message_ids,
        )
        if statement and ids:
            mentions.append(
                {
                    "statement": statement,
                    "status": "unconfirmed",
                    "source_message_ids": ids,
                }
            )

    preferences = [
        text
        for text in (
            _safe_summary_text(item)
            for item in (payload.get("communication_preferences") or [])
        )
        if text
    ]
    return {
        "schema_version": "awm.conversation_summary.v1",
        "tier": tier,
        "coverage": dict(coverage),
        "narrative": _safe_summary_text(payload.get("narrative")),
        "consultation_stage": stage,
        "decisions": _entries(
            "decisions",
            "item",
            {"accepted", "rejected", "revised", "deferred"},
        ),
        "advisor_commitments": _entries(
            "advisor_commitments",
            "commitment",
            {"open", "fulfilled"},
        ),
        "open_questions": _entries("open_questions", "question"),
        "corrections": _entries("corrections", "correction"),
        "communication_preferences": preferences,
        "unconfirmed_mentions": mentions,
    }


class ConversationCompactorAgent:
    def __init__(
        self,
        llm_api_key: str = "",
        model_chain: Optional[List[Tuple[str, str]]] = None,
        prompts_dir: Optional[Path] = None,
        llm_timeout_ms: Optional[int] = None,
        temperature: Optional[float] = None,
        reasoning_effort: Optional[str] = None,
    ) -> None:
        profile = get_task_profile("conversation_compactor")
        self.llm_api_key = llm_api_key
        self.model_chain = model_chain if model_chain is not None else list(profile.chain)
        self.prompts_dir = prompts_dir or task_prompt_dir("conversation_compactor")
        self.llm_timeout_ms = llm_timeout_ms or profile.llm_timeout_ms
        self.temperature = temperature if temperature is not None else profile.temperature
        self.reasoning_effort = reasoning_effort or profile.reasoning_effort

    def compact(
        self,
        inputs: Sequence[Dict[str, Any]],
        tier: int,
        *,
        coverage: Optional[Dict[str, Any]] = None,
        source_summary_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        if tier not in {1, 2}:
            raise ValueError("conversation compaction tier must be 1 or 2")
        normalized_inputs = [dict(item) for item in inputs if isinstance(item, dict)]
        if not normalized_inputs:
            raise ValueError("conversation compaction requires inputs")
        server_coverage = dict(
            coverage
            or (
                _coverage_from_raw_messages(normalized_inputs)
                if tier == 1
                else _coverage_from_summaries(normalized_inputs)
            )
        )
        summary_ids = [
            str(item).strip()
            for item in (source_summary_ids or [])
            if str(item).strip()
        ]

        system_instruction = (self.prompts_dir / "system_prompt.txt").read_text(
            encoding="utf-8"
        )
        user_prompt = json.dumps(
            {
                "tier": tier,
                "server_owned_coverage": server_coverage,
                "source_summary_ids": summary_ids,
                "inputs": normalized_inputs,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        result, model_used, provider_used = self._generate_with_fallback(
            messages=[LLMMessage(role="user", content=user_prompt)],
            system_instruction=system_instruction,
        )
        parsed = self._parse_json_response(result.text)
        summary = validate_conversation_summary(
            parsed,
            tier=tier,
            coverage=server_coverage,
            allowed_source_message_ids=_source_message_ids_from_inputs(
                normalized_inputs,
                tier,
            ),
        )
        return {
            "summary": summary,
            "source_summary_ids": summary_ids,
            "model_used": model_used,
            "provider_used": provider_used,
        }

    def _generate_with_fallback(
        self,
        *,
        messages: List[LLMMessage],
        system_instruction: str,
    ) -> Tuple[LLMGenerateResult, str, str]:
        last_error: Optional[Exception] = None
        for provider_name, model_name in self.model_chain:
            started = time.perf_counter()
            try:
                adapter = LLMClientFactory.create(
                    provider=provider_name,
                    api_key=self.llm_api_key,
                    timeout_ms=self.llm_timeout_ms,
                )
                response = adapter.generate(
                    request=LLMGenerateRequest(
                        messages=messages,
                        system_instruction=system_instruction,
                        temperature=self.temperature,
                        response_schema=CONVERSATION_SUMMARY_SCHEMA,
                        reasoning_effort=self.reasoning_effort,
                    ),
                    model=model_name,
                )
                append_prompt_log(
                    stage="conversation_compactor",
                    provider=provider_name,
                    model=model_name,
                    system_instruction=system_instruction,
                    temperature=self.temperature,
                    contents=serialize_messages(messages),
                    use_tools=False,
                    elapsed_seconds=time.perf_counter() - started,
                    success=True,
                )
                return response, model_name, provider_name
            except Exception as exc:
                last_error = exc
                append_prompt_log(
                    stage="conversation_compactor",
                    provider=provider_name,
                    model=model_name,
                    system_instruction=system_instruction,
                    temperature=self.temperature,
                    contents=serialize_messages(messages),
                    use_tools=False,
                    elapsed_seconds=time.perf_counter() - started,
                    success=False,
                    error=str(exc),
                )
                if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
                    time.sleep(4)
                    continue
                if "404" in str(exc) or "NOT_FOUND" in str(exc):
                    continue
                break
        raise RuntimeError(
            f"ConversationCompactorAgent generation failed: {last_error}"
        )

    @staticmethod
    def _parse_json_response(raw_text: str) -> Dict[str, Any]:
        try:
            parsed = json.loads(str(raw_text or "").strip())
        except json.JSONDecodeError as exc:
            raise ValueError("conversation compactor returned malformed JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("conversation compactor returned a non-object")
        return parsed

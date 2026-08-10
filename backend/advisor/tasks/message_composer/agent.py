"""Message Composer Agent — turns proactive trigger specifications into natural chat bubbles.

This agent handles outbound proactive messages with no user message to respond
to. Inbound user messages are handled by the v2 Companion runtime.

Uses the same LLM infrastructure (LLMClientFactory, model chain fallback) but
has its own system prompt and output schema.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sys

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from advisor.llm.adapter import (
    LLMClientFactory,
    LLMGenerateRequest,
    LLMGenerateResult,
    LLMMessage,
)
from advisor.llm.prompt_logging import append_prompt_log, serialize_messages
from advisor.instructions import task_prompt_dir
from advisor.runtime.task_definition import get_task_profile


# Output schema for structured LLM response
MESSAGE_COMPOSER_SCHEMA: Dict[str, Any] = {
    "title": "proactive_message",
    "type": "object",
    "properties": {
        "bubbles": {
            "type": "array",
            "items": {"type": "string"},
            "description": "1-3 chat bubbles for the proactive message.",
        },
        "push_preview": {
            "type": "string",
            "description": "Short push notification preview text (max 60 chars).",
        },
    },
    "required": ["bubbles", "push_preview"],
}


class MessageComposerAgent:
    """Composes proactive outbound messages in V's voice.

    Takes a structured trigger specification from the ProactivePlanner and
    produces 1-3 natural chat bubbles plus a push notification preview.

    Constructor follows the same lazy-singleton style as the other agent/task
    classes.
    """

    def __init__(
        self,
        llm_api_key: str = "",
        model_chain: Optional[List[Tuple[str, str]]] = None,
        prompts_dir: Optional[Path] = None,
        llm_timeout_ms: Optional[int] = None,
        temperature: Optional[float] = None,
        reasoning_effort: Optional[str] = None,
    ):
        profile = get_task_profile("message_composer")
        self.llm_api_key = llm_api_key
        self.model_chain = model_chain if model_chain is not None else list(profile.chain)
        self.prompts_dir = prompts_dir or task_prompt_dir("message_composer")
        self.llm_timeout_ms = llm_timeout_ms or profile.llm_timeout_ms
        self.temperature = temperature if temperature is not None else profile.temperature
        self.reasoning_effort = reasoning_effort or profile.reasoning_effort

    def compose(
        self,
        triggers: Optional[List[Dict[str, Any]]] = None,
        **single_trigger: Any,
    ) -> Dict[str, Any]:
        """Compose a daily digest from one or more trigger specifications.

        Args:
            triggers: List of trigger dicts, each with keys:
                trigger_class, trigger_type, trigger_reason, guidance_mode,
                objective, grounding_facts, escalation_level, allowed_cta.
                Ordered by priority (operational → advisory → relational).

        Returns:
            {
                "bubbles": ["bubble 1", "bubble 2", ...],  # 2-3 total
                "push_preview": "short preview text",
            }
        """
        if triggers is None:
            triggers = [single_trigger] if single_trigger else []
        if not triggers:
            raise ValueError("compose() requires at least one trigger")

        system_prompt = self._read_prompt("system_prompt.txt")
        user_prompt = self._build_user_prompt(triggers)

        messages = [LLMMessage(role="user", content=user_prompt)]

        result, model_used, provider_used = self._generate_with_fallback(
            messages=messages,
            system_instruction=system_prompt,
            temperature=self.temperature,
            response_schema=MESSAGE_COMPOSER_SCHEMA,
        )

        parsed = self._parse_json_response(result.text)

        bubbles = parsed.get("bubbles", [])
        if not isinstance(bubbles, list) or not bubbles:
            bubbles = [parsed.get("push_preview", "hey, had a few things I wanted to share")]
        bubbles = [str(b) for b in bubbles[:3] if b]

        push_preview = str(parsed.get("push_preview", "V has a message for you"))
        if len(push_preview) > 80:
            push_preview = push_preview[:77] + "..."

        return {
            "bubbles": bubbles,
            "push_preview": push_preview,
            "model_used": model_used,
            "provider_used": provider_used,
        }

    @staticmethod
    def _build_user_prompt(
        triggers: Optional[List[Dict[str, Any]]] = None,
        **single_trigger: Any,
    ) -> str:
        """Build the user prompt from a list of trigger specifications."""
        if triggers is None:
            triggers = [single_trigger] if single_trigger else []
        lines = [f"## Today's Triggers ({len(triggers)} item(s))\n"]

        for i, t in enumerate(triggers, 1):
            facts_text = json.dumps(t.get("grounding_facts", []), indent=2) or "(none)"
            escalation_level = t.get("escalation_level", 0)
            if escalation_level == 0:
                escalation_note = "FIRST time mentioning this — be gentle."
            elif escalation_level == 1:
                escalation_note = "SECOND time mentioning this — a bit more direct but still warm."
            else:
                escalation_note = "THIRD and FINAL mention — be clear about why it matters."

            allowed_cta = t.get("allowed_cta", "")
            cta_note = (
                f"If you include a call-to-action, guide toward: {allowed_cta}"
                if allowed_cta
                else "Do NOT include any call-to-action — purely relational."
            )

            lines.append(
                f"### Trigger {i}: {t.get('trigger_type', '')}\n"
                f"- Class: {t.get('trigger_class', '')}\n"
                f"- Why: {t.get('trigger_reason', '')}\n"
                f"- Guidance mode: {t.get('guidance_mode', '')}\n"
                f"- Objective: {t.get('objective', '')}\n"
                f"- {escalation_note}\n"
                f"- {cta_note}\n"
                f"- Facts:\n{facts_text}\n"
            )

        lines.append(
            "Compose a single cohesive morning check-in message now. "
            "Weave all the above into one flowing conversation — not a bullet list. "
            "Output strict JSON only."
        )
        return "\n".join(lines)

    def _generate_with_fallback(
        self,
        messages: List[LLMMessage],
        system_instruction: str,
        temperature: Optional[float] = None,
        stage_name: str = "message_composer",
        response_schema: Optional[Dict[str, Any]] = None,
    ) -> Tuple[LLMGenerateResult, str, str]:
        """Call LLM with provider fallback chain."""
        last_error = None
        for provider_name, model_name in self.model_chain:
            started = time.perf_counter()
            try:
                adapter = LLMClientFactory.create(
                    provider=provider_name,
                    api_key=self.llm_api_key,
                    timeout_ms=self.llm_timeout_ms,
                )
                request = LLMGenerateRequest(
                    messages=messages,
                    system_instruction=system_instruction,
                    temperature=temperature,
                    response_schema=response_schema,
                    reasoning_effort=self.reasoning_effort,
                )
                response = adapter.generate(request=request, model=model_name)
                append_prompt_log(
                    stage=stage_name,
                    provider=provider_name,
                    model=model_name,
                    system_instruction=system_instruction,
                    temperature=temperature,
                    contents=serialize_messages(messages),
                    use_tools=False,
                    elapsed_seconds=time.perf_counter() - started,
                    success=True,
                )
                return response, model_name, provider_name
            except Exception as exc:
                append_prompt_log(
                    stage=stage_name,
                    provider=provider_name,
                    model=model_name,
                    system_instruction=system_instruction,
                    temperature=temperature,
                    contents=serialize_messages(messages),
                    use_tools=False,
                    elapsed_seconds=time.perf_counter() - started,
                    success=False,
                    error=str(exc),
                )
                last_error = exc
                message = str(exc)
                if "429" in message or "RESOURCE_EXHAUSTED" in message:
                    time.sleep(4)
                    continue
                if "404" in message or "NOT_FOUND" in message:
                    continue
                break
        raise RuntimeError(
            f"MessageComposerAgent LLM generation failed: {last_error}"
        )

    def _read_prompt(self, filename: str) -> str:
        """Load a prompt file from the prompts directory."""
        path = self.prompts_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Prompt file not found: {path}")
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _parse_json_response(raw_text: str) -> Dict[str, Any]:
        """Parse JSON from LLM response."""
        text = raw_text.strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start: end + 1])
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        print(f"[MessageComposer] Failed to parse JSON, using fallback", flush=True)
        return {
            "bubbles": ["hey, had something I wanted to share with you"],
            "push_preview": "V has a message for you",
        }

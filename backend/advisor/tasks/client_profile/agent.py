"""Client Profile Extractor — pure LLM fact extraction from consultation transcripts.

No tools, no gap analysis. Extracts candidate facts from unstructured transcripts
and feeds them to the Knowledge Updater Agent for validation and commitment.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from advisor.llm.adapter import (
    LLMClientFactory,
    LLMGenerateRequest,
    LLMMessage,
)
from advisor.llm.prompt_logging import append_prompt_log, serialize_messages
from advisor.llm.schemas import CLIENT_PROFILE_SCHEMA
from advisor.instructions import task_prompt_dir
from advisor.runtime.task_definition import get_task_profile


class ClientProfileExtractor:
    """Pure LLM extraction of candidate facts from consultation transcripts.

    Does NOT extend ToolLoopRunner — no tools, no gap analysis, no ReAct loop.
    Input: consultation transcript + session metadata.
    Output: list of candidate facts in knowledge_fact shape.
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
        profile = get_task_profile("client_profile")
        self.llm_api_key = llm_api_key
        self.model_chain = model_chain if model_chain is not None else list(profile.chain)
        self.prompts_dir = prompts_dir or task_prompt_dir("client_profile")
        self.llm_timeout_ms = llm_timeout_ms or profile.llm_timeout_ms
        self.temperature = temperature if temperature is not None else profile.temperature
        self.reasoning_effort = reasoning_effort or profile.reasoning_effort

    def extract_facts(
        self,
        transcript: Dict[str, Any],
        session_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Extract candidate facts from a consultation transcript.

        Args:
            transcript: The consultation transcript (turns, metadata, etc.)
            session_metadata: Optional session context (session_type, journey_type, etc.)

        Returns:
            {
                "candidate_facts": [
                    {
                        "domain": "wealth",
                        "category": "income",
                        "label": "Annual salary",
                        "value": 150000,
                        "confidence": 0.9,
                        "evidence_text": "Client stated they earn $150k per year",
                    },
                    ...
                ],
                "model_used": str,
                "provider_used": str,
            }
        """
        system_prompt = self._read_prompt("system_prompt.txt")

        # Build user message with transcript and context
        user_content = self._build_extraction_prompt(transcript, session_metadata)

        messages = [LLMMessage(role="user", content=user_content)]

        result, model_used, provider_used = self._generate_with_fallback(
            messages=messages,
            system_instruction=system_prompt,
            temperature=self.temperature,
            response_schema=CLIENT_PROFILE_SCHEMA,
        )

        # Parse the JSON response (structured output guarantees valid JSON,
        # but _parse_json_response is kept as a safety net for fallback providers)
        parsed = self._parse_json_response(result.text)

        return {
            "candidate_facts": parsed.get("candidate_facts", []),
            "model_used": model_used,
            "provider_used": provider_used,
        }

    def _build_extraction_prompt(
        self,
        transcript: Dict[str, Any],
        session_metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Build the user prompt for fact extraction."""
        parts = []

        if session_metadata:
            parts.append(f"## Session Context\n{json.dumps(session_metadata, indent=2)}")

        # Format transcript turns for the LLM
        turns = transcript.get("turns", [])
        if turns:
            formatted_turns = []
            for turn in turns:
                role = turn.get("role", "unknown")
                text = turn.get("text", turn.get("content", ""))
                formatted_turns.append(f"[{role}]: {text}")
            parts.append(f"## Consultation Transcript\n" + "\n".join(formatted_turns))
        else:
            # Fallback: pass the whole transcript as JSON
            parts.append(f"## Consultation Transcript\n{json.dumps(transcript, indent=2)}")

        parts.append(
            "## Task\n"
            "Extract all client facts from the transcript above. "
            "Return a JSON object with a single key `candidate_facts` containing "
            "an array of fact objects."
        )

        return "\n\n".join(parts)

    def _generate_with_fallback(
        self,
        messages: List[LLMMessage],
        system_instruction: str,
        temperature: Optional[float] = None,
        response_schema: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Any, str, str]:
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
                    stage="client_profile_extraction",
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
                    stage="client_profile_extraction",
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
            f"ClientProfileExtractor LLM generation failed after all providers: {last_error}"
        )

    def _read_prompt(self, filename: str) -> str:
        """Load a prompt file from the prompts directory."""
        path = self.prompts_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Prompt file not found: {path}")
        return path.read_text(encoding="utf-8")

    def _parse_json_response(self, raw_text: str) -> Dict[str, Any]:
        """Parse JSON from LLM response, with fallback extraction."""
        text = raw_text.strip()
        # Try direct parse
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        # Fallback: extract { ... } from text
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        print(f"[ClientProfileExtractor] Failed to parse JSON from response", flush=True)
        return {"candidate_facts": []}

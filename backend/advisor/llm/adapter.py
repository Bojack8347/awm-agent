"""Provider-neutral LLM abstraction for AWM advisor tasks (DeepSeek)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Sequence, Tuple

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]

try:
    from dotenv import dotenv_values
except ImportError:  # pragma: no cover
    dotenv_values = None  # type: ignore[assignment]

from advisor.llm.schema_validation import LLMSchemaValidationError, enforce_schema


@dataclass
class ToolCall:
    """Normalized tool call emitted by an assistant response."""

    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolSchema:
    """Provider-neutral tool function schema."""

    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    required: List[str] = field(default_factory=list)


@dataclass
class LLMMessage:
    """Provider-neutral chat message."""

    role: str
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


@dataclass
class LLMGenerateRequest:
    """Normalized generation request."""

    messages: List[LLMMessage]
    system_instruction: str = ""
    # None means "use the provider/model default" and no temperature field is
    # sent. Runtime profiles own that choice; adapters only serialize it.
    temperature: Optional[float] = 0.2
    tools: List[ToolSchema] = field(default_factory=list)
    # Optional JSON schema dict to enforce structured output.
    # When set, the provider will constrain the model to produce valid JSON
    # matching this schema.  Tool-use requests ignore this field.
    response_schema: Optional[Dict[str, Any]] = None
    # Kept because task profiles still declare an effort per stage. DeepSeek
    # models reason natively with no effort knob, so adapters drop it rather
    # than inventing a fake budget.
    reasoning_effort: Optional[str] = None


@dataclass
class LLMGenerateResult:
    """Normalized generation response."""

    provider: str
    model: str
    text: str
    messages: List[LLMMessage]
    tool_calls: List[ToolCall]
    response_id: Optional[str] = None
    raw: Any = None


@dataclass
class LLMStreamEvent:
    """Provider-neutral event emitted by a streaming model response.

    AWM keeps the legacy `generate_stream()` text-only API for companion SSE,
    but tool-enabled agent loops need a richer event surface: a provider can
    finish a function-call argument block before the whole response is
    complete, which lets the tool loop start that HTTP tool early.
    """

    type: str
    text: str = ""
    tool_call: Optional[ToolCall] = None
    result: Optional[LLMGenerateResult] = None
    response_id: Optional[str] = None


class LLMAdapter:
    """Adapter interface."""

    provider: str

    def generate(self, request: LLMGenerateRequest, model: str) -> LLMGenerateResult:
        raise NotImplementedError

    def _enforce_response_schema(
        self, request: LLMGenerateRequest, result: LLMGenerateResult
    ) -> None:
        """Validate structured output in AWM rather than trusting the provider.

        Provider-side enforcement is not universally available — DeepSeek has
        no strict mode and only offers ``json_object`` — so every adapter
        checks its own result. Raises
        :class:`LLMSchemaValidationError` on violation.
        """
        enforce_schema(
            result.text,
            request.response_schema,
            has_tools=bool(request.tools),
            provider=self.provider,
            model=result.model or "",
        )

    def generate_stream(self, request: LLMGenerateRequest, model: str) -> Generator[str, None, None]:
        """Yield text chunks as they arrive from the model. Default falls back to non-streaming."""
        for event in self.generate_events(request=request, model=model):
            if event.type == "text_delta" and event.text:
                yield event.text

    def generate_events(
        self,
        request: LLMGenerateRequest,
        model: str,
    ) -> Generator[LLMStreamEvent, None, None]:
        """Yield typed streaming events.

        Provider adapters that cannot expose incremental tool calls safely may
        fall back to one full `generate()` call. This preserves correctness and
        lets callers opt in only when a provider has enough streaming metadata.
        """
        result = self.generate(request, model)
        if result.text:
            yield LLMStreamEvent(type="text_delta", text=result.text, response_id=result.response_id)
        for call in result.tool_calls:
            yield LLMStreamEvent(type="tool_call", tool_call=call, response_id=result.response_id)
        yield LLMStreamEvent(type="done", result=result, response_id=result.response_id)


DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"


class DeepSeekAdapter(LLMAdapter):
    """DeepSeek adapter over the OpenAI-compatible Chat Completions surface.

    DeepSeek speaks Chat Completions, not the OpenAI Responses API, so this
    maps AWM's neutral types onto that surface directly. Probed
    against ``deepseek-v4-flash`` / ``deepseek-v4-pro``: tool calling, parallel
    tool calls, streaming tool-call deltas, ``json_object`` mode and
    ``temperature`` all work; strict ``json_schema`` is rejected with HTTP 400.

    Responses-only concepts are deliberately dropped rather than faked:
    ``previous_response_id`` (DeepSeek is stateless — history is resent),
    ``store``, and ``prompt_cache_key`` (caching is automatic and reported via
    ``prompt_cache_hit_tokens``). ``reasoning_effort`` is dropped too: these
    models reason natively with no effort knob.
    """

    provider = "deepseek"

    def __init__(
        self,
        api_key: str,
        timeout_s: int = 90,
        base_url: str = DEEPSEEK_DEFAULT_BASE_URL,
    ):
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=float(timeout_s))

    def _to_tools(self, tools: Sequence[ToolSchema]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for tool in tools:
            schema: Dict[str, Any] = {"type": "object", "properties": tool.parameters or {}}
            if tool.required:
                schema["required"] = list(tool.required)
            rows.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": schema,
                    },
                }
            )
        return rows

    def _to_messages(
        self, messages: Sequence[LLMMessage], system_instruction: str
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if system_instruction:
            rows.append({"role": "system", "content": system_instruction})
        for message in messages:
            role = str(message.role or "").strip().lower()
            if role == "assistant":
                row: Dict[str, Any] = {"role": "assistant", "content": message.content or ""}
                if message.tool_calls:
                    row["tool_calls"] = [
                        {
                            "id": str(call.id or "call_missing"),
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=True),
                            },
                        }
                        for call in message.tool_calls
                    ]
                rows.append(row)
                continue

            if role == "tool":
                rows.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(message.tool_call_id or "tool_call_missing"),
                        "content": message.content or "{}",
                    }
                )
                continue

            rows.append(
                {
                    "role": role if role in {"user", "system"} else "user",
                    "content": message.content or "",
                }
            )
        return rows

    @staticmethod
    def _ensure_json_hint(
        rows: List[Dict[str, Any]], schema: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """State the contract in the prompt, since the provider cannot enforce it.

        ``json_object`` mode only guarantees syntactically valid JSON — it does
        not constrain the shape the way a strict ``json_schema`` mode would. It
        also requires the word JSON to appear in the prompt. Passing the schema
        itself is what gets conformance close enough for AWM's validator to pass
        instead of rejecting nearly every reply; the validator remains the
        actual guarantee.
        """
        return rows + [
            {
                "role": "system",
                "content": (
                    "Respond with a single valid JSON object and nothing else. "
                    "It must conform exactly to this JSON Schema — obey every "
                    "type, required field, and enum, and emit no properties "
                    "outside it:\n"
                    f"{json.dumps(schema, ensure_ascii=False)}"
                ),
            }
        ]

    def _build_params(self, request: LLMGenerateRequest, model: str) -> Dict[str, Any]:
        rows = self._to_messages(request.messages, request.system_instruction)
        if request.response_schema and not request.tools:
            rows = self._ensure_json_hint(rows, request.response_schema)
        params: Dict[str, Any] = {"model": model, "messages": rows}
        if request.temperature is not None:
            params["temperature"] = request.temperature
        if request.tools:
            params["tools"] = self._to_tools(request.tools)
            params["tool_choice"] = "auto"
        elif request.response_schema:
            # Strict json_schema is unavailable on DeepSeek; callers validate.
            params["response_format"] = {"type": "json_object"}
        return params

    @staticmethod
    def _parse_args(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, str) and raw.strip():
            try:
                loaded = json.loads(raw)
            except json.JSONDecodeError:
                return {}
            if isinstance(loaded, dict):
                return loaded
        return {}

    def generate(self, request: LLMGenerateRequest, model: str) -> LLMGenerateResult:
        response = self.client.chat.completions.create(**self._build_params(request, model))
        choice = (getattr(response, "choices", None) or [None])[0]
        message = getattr(choice, "message", None)
        text = str(getattr(message, "content", "") or "").strip()

        calls: List[ToolCall] = []
        for raw_call in getattr(message, "tool_calls", None) or []:
            function = getattr(raw_call, "function", None)
            calls.append(
                ToolCall(
                    id=str(getattr(raw_call, "id", "") or "call_missing"),
                    name=str(getattr(function, "name", "") or ""),
                    arguments=self._parse_args(getattr(function, "arguments", "")),
                )
            )

        out_messages: List[LLMMessage] = []
        if text or calls:
            out_messages.append(LLMMessage(role="assistant", content=text, tool_calls=calls))

        result = LLMGenerateResult(
            provider=self.provider,
            model=model,
            text=text,
            messages=out_messages,
            tool_calls=calls,
            response_id=str(getattr(response, "id", "") or "") or None,
            raw=response,
        )
        self._enforce_response_schema(request, result)
        return result

    def generate_events(
        self,
        request: LLMGenerateRequest,
        model: str,
    ) -> Generator[LLMStreamEvent, None, None]:
        params = self._build_params(request, model)
        params["stream"] = True
        stream = self.client.chat.completions.create(**params)

        text_parts: List[str] = []
        # Chat Completions splits each tool call across deltas keyed by index.
        slots: Dict[int, Dict[str, str]] = {}
        response_id: Optional[str] = None

        for chunk in stream:
            response_id = response_id or (str(getattr(chunk, "id", "") or "") or None)
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            if delta is None:
                continue

            piece = str(getattr(delta, "content", "") or "")
            if piece:
                text_parts.append(piece)
                yield LLMStreamEvent(type="text_delta", text=piece, response_id=response_id)

            for raw_call in getattr(delta, "tool_calls", None) or []:
                index = int(getattr(raw_call, "index", 0) or 0)
                slot = slots.setdefault(index, {"id": "", "name": "", "arguments": ""})
                call_id = str(getattr(raw_call, "id", "") or "")
                if call_id:
                    slot["id"] = call_id
                function = getattr(raw_call, "function", None)
                if function is not None:
                    name = str(getattr(function, "name", "") or "")
                    if name:
                        slot["name"] = name
                    slot["arguments"] += str(getattr(function, "arguments", "") or "")

        calls = [
            ToolCall(
                id=slot["id"] or "call_missing",
                name=slot["name"],
                arguments=self._parse_args(slot["arguments"]),
            )
            for _, slot in sorted(slots.items())
            if slot["name"]
        ]
        for call in calls:
            yield LLMStreamEvent(type="tool_call", tool_call=call, response_id=response_id)

        text = "".join(text_parts).strip()
        out_messages: List[LLMMessage] = []
        if text or calls:
            out_messages.append(LLMMessage(role="assistant", content=text, tool_calls=calls))
        yield LLMStreamEvent(
            type="done",
            result=LLMGenerateResult(
                provider=self.provider,
                model=model,
                text=text,
                messages=out_messages,
                tool_calls=calls,
                response_id=response_id,
            ),
            response_id=response_id,
        )


def _deepseek_api_key_from_env() -> str:
    """Resolve the DeepSeek key from env, repo .env, or the local key file."""
    for name in ("DEEPSEEK_API_KEY", "AWM_DEEPSEEK_API_KEY"):
        value = str(os.getenv(name) or "").strip()
        if value:
            return value

    repo_root = Path(__file__).resolve().parents[3]
    if dotenv_values is not None:
        for env_path in (repo_root / ".env", repo_root / ".evn"):
            if not env_path.exists():
                continue
            parsed = dotenv_values(env_path)
            for name in ("DEEPSEEK_API_KEY", "AWM_DEEPSEEK_API_KEY"):
                value = str(parsed.get(name) or "").strip()
                if value:
                    return value

    key_file = repo_root / "dsk_api.txt"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    return ""


class LLMClientFactory:
    """Factory for provider adapters."""

    @staticmethod
    def create(
        provider: str,
        api_key: str = "",
        timeout_ms: int = 90000,
    ) -> LLMAdapter:
        normalized = str(provider or "").strip().lower()
        timeout_s = max(1, int(timeout_ms / 1000))
        if normalized == "deepseek":
            key = str(api_key or "").strip() or _deepseek_api_key_from_env()
            if not key:
                raise ValueError("DeepSeek API key missing (set DEEPSEEK_API_KEY)")
            base_url = str(os.getenv("DEEPSEEK_BASE_URL") or "").strip() or DEEPSEEK_DEFAULT_BASE_URL
            return DeepSeekAdapter(api_key=key, timeout_s=timeout_s, base_url=base_url)
        raise ValueError(f"Unsupported LLM provider: {provider}")


def parse_fallback_chain(raw: str) -> List[Tuple[str, str]]:
    """Parse fallback list in `provider:model` CSV format."""
    rows: List[Tuple[str, str]] = []
    for token in str(raw or "").split(","):
        item = token.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(
                f"Invalid fallback entry '{item}'. Expected format provider:model"
            )
        provider, model = item.split(":", 1)
        provider = provider.strip().lower()
        model = model.strip()
        if not provider or not model:
            raise ValueError(
                f"Invalid fallback entry '{item}'. Expected format provider:model"
            )
        if provider != "deepseek":
            raise ValueError(
                f"Unsupported fallback provider '{provider}'. Supported: deepseek."
            )
        rows.append((provider, model))
    return rows


def dedupe_model_chain(primary: Tuple[str, str], fallbacks: Sequence[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Build ordered unique model chain."""
    chain: List[Tuple[str, str]] = []
    for candidate in [primary, *fallbacks]:
        provider = str(candidate[0] or "").strip().lower()
        model = str(candidate[1] or "").strip()
        if not provider or not model:
            continue
        row = (provider, model)
        if row not in chain:
            chain.append(row)
    return chain

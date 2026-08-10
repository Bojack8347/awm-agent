"""Live, domain-restricted web-search gateway for assumption research."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from enum import Enum
from threading import RLock
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from advisor.assumptions.providers.registry import (
    build_default_provider_registry,
)
from advisor.assumptions.promotion import (
    AutomaticPublicFactPromotionService,
)
from advisor.assumptions.research import (
    InMemoryResearchAttemptLedger,
    ResearchErrorCode,
    ResearchFinding,
    ResearchRequest,
    ResearchRule,
    ResearchSource,
    ResearchSpecialist,
    ResearchSpecialistError,
)


class AssumptionResearchMode(str, Enum):
    OFF = "off"
    LIVE = "live"


def assumption_research_mode() -> AssumptionResearchMode:
    raw = os.getenv("AWM_ASSUMPTION_RESEARCH_MODE", "off").strip().lower()
    try:
        return AssumptionResearchMode(raw)
    except ValueError as exc:
        raise RuntimeError(
            "AWM_ASSUMPTION_RESEARCH_MODE must be one of: off, live"
        ) from exc


class _GatewaySource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    publisher: str
    title: str
    url: str
    published_at: str | None


class _GatewayPayload(BaseModel):
    """Strict wire format; arbitrary values travel as encoded JSON."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    found: bool
    value_json: str | None
    unit: str | None
    jurisdiction: str | None
    sources: tuple[_GatewaySource, ...] = Field(max_length=3)
    failure_reason: str | None

    @model_validator(mode="after")
    def validate_result_shape(self) -> "_GatewayPayload":
        if self.found:
            if (
                self.value_json is None
                or self.unit is None
                or self.jurisdiction is None
                or not self.sources
                or self.failure_reason is not None
            ):
                raise ValueError("found research output is incomplete")
        elif (
            self.value_json is not None
            or self.unit is not None
            or self.jurisdiction is not None
            or self.sources
            or not self.failure_reason
        ):
            raise ValueError("not-found research output is inconsistent")
        return self


class OpenAIWebResearchGateway:
    """One-call Responses API gateway with primary-source restrictions."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-5.6-luna",
        timeout_seconds: float = 45.0,
        client: Any | None = None,
    ) -> None:
        if not str(api_key or "").strip() and client is None:
            raise ValueError("OPENAI_API_KEY is required for live research")
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError(
                "research timeout_seconds must be between 0 and 120"
            )
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                api_key=api_key,
                timeout=timeout_seconds,
                max_retries=0,
            )
        self.client = client
        self.model = str(model or "").strip() or "gpt-5.6-luna"

    def find(
        self,
        request: ResearchRequest,
        *,
        rule: ResearchRule,
    ) -> ResearchFinding | None:
        schema = _GatewayPayload.model_json_schema()
        try:
            response = self.client.responses.create(
                model=self.model,
                reasoning={"effort": "low"},
                instructions=(
                    "You are AWM's silent public-data Research Specialist. "
                    "Research exactly one public financial-planning variable. "
                    "Use only the configured authoritative domains. Never infer "
                    "client facts, choose planning assumptions, give advice, or "
                    "calculate a personalized recommendation. Search once. "
                    "Return found=false when the exact effective-year value is "
                    "not unambiguously supported. Encode the normalized value "
                    "as JSON inside value_json. For scalar units (USD_annual, "
                    "USD_per_month, or percent), encode a bare JSON number, not "
                    "an object, quoted string, or formatted currency. Use the exact requested "
                    "unit, jurisdiction, and allowlisted publisher label."
                ),
                input=(
                    f"Variable key: {request.variable_key}\n"
                    f"Meaning: {rule.label}\n"
                    f"Effective year: {request.effective_year}\n"
                    f"Jurisdiction: {rule.jurisdiction}\n"
                    f"Required unit: {rule.expected_unit}\n"
                    "Allowed publisher labels: "
                    f"{', '.join(rule.allowed_publishers)}\n"
                    "Return no more than three primary-source records."
                ),
                tools=[
                    {
                        "type": "web_search",
                        "search_context_size": "low",
                        "filters": {
                            "allowed_domains": list(rule.allowed_domains),
                        },
                    }
                ],
                tool_choice="required",
                max_tool_calls=1,
                parallel_tool_calls=False,
                include=["web_search_call.action.sources"],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "awm_assumption_research_finding",
                        "schema": schema,
                        "strict": True,
                    }
                },
                store=False,
            )
        except Exception as exc:
            code, message = _classified_gateway_failure(exc)
            raise ResearchSpecialistError(
                code,
                message,
                variable_key=request.variable_key,
                effective_year=request.effective_year,
                attempted=True,
            ) from exc
        output_text = str(getattr(response, "output_text", "") or "").strip()
        try:
            payload = _GatewayPayload.model_validate_json(output_text)
        except Exception as exc:
            raise ResearchSpecialistError(
                ResearchErrorCode.OUTPUT_INVALID,
                "research gateway returned invalid structured output",
                variable_key=request.variable_key,
                effective_year=request.effective_year,
                attempted=True,
            ) from exc
        if not payload.found:
            return None

        try:
            value = _normalized_gateway_value(
                json.loads(payload.value_json or ""),
                expected_unit=rule.expected_unit,
            )
        except json.JSONDecodeError as exc:
            raise ResearchSpecialistError(
                ResearchErrorCode.OUTPUT_INVALID,
                "research value_json is invalid",
                variable_key=request.variable_key,
                effective_year=request.effective_year,
                attempted=True,
            ) from exc

        consulted_urls, saw_search = _response_source_urls(response)
        if not saw_search or not consulted_urls:
            raise ResearchSpecialistError(
                ResearchErrorCode.OUTPUT_INVALID,
                "research response has no verifiable web-search sources",
                variable_key=request.variable_key,
                effective_year=request.effective_year,
                attempted=True,
            )
        source_urls = {
            _normalized_source_url(source.url) for source in payload.sources
        }
        if not source_urls.issubset(consulted_urls):
            raise ResearchSpecialistError(
                ResearchErrorCode.SOURCE_NOT_ALLOWED,
                "reported research evidence was not consulted by web search",
                variable_key=request.variable_key,
                effective_year=request.effective_year,
                attempted=True,
            )

        try:
            sources = tuple(
                    ResearchSource(
                        publisher=source.publisher,
                        title=source.title,
                        url=source.url,
                        published_at=_normalized_published_at(
                            source.published_at
                        ),
                    )
                for source in payload.sources
            )
            return ResearchFinding(
                variable_key=request.variable_key,
                effective_year=request.effective_year,
                value=value,
                unit=payload.unit,
                jurisdiction=payload.jurisdiction,
                sources=sources,
            )
        except Exception as exc:
            raise ResearchSpecialistError(
                ResearchErrorCode.OUTPUT_INVALID,
                "research finding failed contract validation",
                variable_key=request.variable_key,
                effective_year=request.effective_year,
                attempted=True,
            ) from exc


def _response_source_urls(response: Any) -> tuple[set[str], bool]:
    urls: set[str] = set()
    saw_search = False
    for item in getattr(response, "output", None) or ():
        data = _as_mapping(item)
        item_type = str(data.get("type") or "")
        if item_type == "web_search_call":
            saw_search = True
            urls.update(_urls_in_value(data.get("action")))
        elif item_type == "message":
            for content in data.get("content") or ():
                content_data = _as_mapping(content)
                for annotation in content_data.get("annotations") or ():
                    annotation_data = _as_mapping(annotation)
                    url = annotation_data.get("url")
                    if url:
                        urls.add(_normalized_source_url(str(url)))
    return urls, saw_search


def _classified_gateway_failure(
    exc: Exception,
) -> tuple[ResearchErrorCode, str]:
    """Translate SDK failures without exposing provider error details."""

    status_code = getattr(exc, "status_code", None)
    provider_code = getattr(exc, "code", None)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error_body = body.get("error", body)
        if isinstance(error_body, dict):
            provider_code = error_body.get("code") or provider_code

    if status_code in {401, 403}:
        return (
            ResearchErrorCode.GATEWAY_AUTHENTICATION_FAILED,
            "research gateway authentication or authorization failed",
        )
    if status_code == 429 and provider_code == "insufficient_quota":
        return (
            ResearchErrorCode.GATEWAY_QUOTA_EXHAUSTED,
            "research gateway quota is exhausted",
        )
    if status_code == 429:
        return (
            ResearchErrorCode.GATEWAY_RATE_LIMITED,
            "research gateway is temporarily rate limited",
        )
    return (
        ResearchErrorCode.GATEWAY_UNAVAILABLE,
        "research gateway is unavailable",
    )


def _urls_in_value(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple)):
        urls: set[str] = set()
        for item in value:
            urls.update(_urls_in_value(item))
        return urls
    data = _as_mapping(value)
    urls = set()
    url = data.get("url")
    if url:
        urls.add(_normalized_source_url(str(url)))
    for nested in data.values():
        if isinstance(nested, (dict, list, tuple)) or hasattr(
            nested,
            "model_dump",
        ):
            urls.update(_urls_in_value(nested))
    return urls


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        dumped = dump(mode="python")
        return dumped if isinstance(dumped, dict) else {}
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {}


def _normalized_source_url(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme.lower()}://{host}{path}"


def _normalized_published_at(value: str | None) -> datetime | None:
    """Normalize an authority's calendar publication date for audit storage."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            published_date = date.fromisoformat(text)
            return datetime.combine(
                published_date,
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("published_at must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("published_at timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _normalized_gateway_value(value: Any, *, expected_unit: str) -> Any:
    """Unwrap the sole value field for an otherwise scalar wire result."""

    if (
        expected_unit in {"USD_annual", "USD_per_month", "percent"}
        and isinstance(value, dict)
        and set(value) == {"value"}
    ):
        return value["value"]
    return value


_RUNTIME_LOCK = RLock()
_RUNTIME_GATEWAY: OpenAIWebResearchGateway | None = None
_RUNTIME_LEDGER = InMemoryResearchAttemptLedger()


class _LazyRuntimeResearchGateway:
    """Avoid opening the network path unless deterministic sourcing failed."""

    def find(
        self,
        request: ResearchRequest,
        *,
        rule: ResearchRule,
    ) -> ResearchFinding | None:
        global _RUNTIME_GATEWAY
        with _RUNTIME_LOCK:
            if _RUNTIME_GATEWAY is None:
                api_key = str(
                    os.getenv("OPENAI_API_KEY", "") or ""
                ).strip()
                if not api_key:
                    raise RuntimeError(
                        "OPENAI_API_KEY is required for live research"
                    )
                timeout = float(
                    os.getenv(
                        "AWM_ASSUMPTION_RESEARCH_TIMEOUT_SECONDS",
                        "45",
                    )
                )
                _RUNTIME_GATEWAY = OpenAIWebResearchGateway(
                    api_key=api_key,
                    model=os.getenv(
                        "AWM_ASSUMPTION_RESEARCH_MODEL",
                        "gpt-5.6-luna",
                    ),
                    timeout_seconds=timeout,
                )
            gateway = _RUNTIME_GATEWAY
        return gateway.find(request, rule=rule)


def build_runtime_research_specialist(
    *,
    repository: Any,
) -> ResearchSpecialist | None:
    """Build the non-agent fallback; live access is explicitly opt-in."""

    if assumption_research_mode() is AssumptionResearchMode.OFF:
        return None
    return ResearchSpecialist(
        gateway=_LazyRuntimeResearchGateway(),
        repository=repository,
        validators=build_default_provider_registry(),
        attempt_ledger=_RUNTIME_LEDGER,
    )


def build_runtime_session_research_specialist(
    *,
    repository: Any | None = None,
) -> ResearchSpecialist | None:
    """Build the ephemeral Financial Planning research boundary when enabled."""

    if assumption_research_mode() is AssumptionResearchMode.OFF:
        return None
    return ResearchSpecialist(
        gateway=_LazyRuntimeResearchGateway(),
        repository=repository,
        validators=build_default_provider_registry(),
    )


def build_runtime_public_fact_promotion_service(
    *,
    repository: Any,
) -> AutomaticPublicFactPromotionService:
    """Build the deterministic verifier/persistence boundary; no agent routing."""

    return AutomaticPublicFactPromotionService(
        repository=repository,
        providers=build_default_provider_registry(),
    )


__all__ = [
    "AssumptionResearchMode",
    "OpenAIWebResearchGateway",
    "assumption_research_mode",
    "build_runtime_research_specialist",
    "build_runtime_public_fact_promotion_service",
    "build_runtime_session_research_specialist",
]

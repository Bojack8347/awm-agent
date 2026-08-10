"""Validated definitions for one-shot task runtimes.

Task metadata and LLM settings are declared beside each task prompt in
``task_contract.txt``. Unlike SDK agents, tasks make one bounded model call,
do not expose tools, and do not run a ReAct loop.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from advisor.instructions import TASK_INSTRUCTION_ROOT
from advisor.instructions.manifest import read_frontmatter

ProviderModel = Tuple[str, str]

TASK_REQUIRED_FIELDS = {
    "key",
    "name",
    "description",
    "module",
    "entrypoint",
    "default_model",
    "reasoning_effort",
    "temperature",
    "fallback_models",
    "timeout_seconds",
    "allowed_callers",
    "side_effect_level",
    "execution",
    "idempotent",
    "max_runtime_seconds",
}
TASK_OPTIONAL_FIELDS = {
    "output_contract",
    "requires_user_visible_progress",
}
VALID_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh"}
VALID_EXECUTION_MODES = {"wait", "background"}
VALID_SIDE_EFFECT_LEVELS = {"none", "writes_profile", "writes_journey", "external"}

SUPPORTED_TASK_PROVIDERS = {"deepseek"}
DEFAULT_TASK_PROVIDER = "deepseek"
DEFAULT_DEEPSEEK_TASK_MODEL = "deepseek-v4-flash"


def _apply_provider_override(
    model: str, fallback_models: Tuple[str, ...]
) -> Tuple[str, str, Tuple[str, ...]]:
    """Route every one-shot task at one provider via ``AWM_TASK_LLM_PROVIDER``.

    ``task_contract.txt`` still declares legacy OpenAI model names, so the
    provider's own default model is substituted — a DeepSeek endpoint cannot
    serve ``gpt-5.6-luna``. ``AWM_TASK_LLM_MODEL`` overrides that substitution.
    Declared fallback chains are dropped rather than translated: those names
    are OpenAI-specific and would 404 here.

    Unknown provider names fail loudly here instead of silently running on the
    default, matching how ``model_profiles`` validates ``AWM_MODEL_PROFILE``.
    """
    provider = (
        os.getenv("AWM_TASK_LLM_PROVIDER", "") or ""
    ).strip().lower() or DEFAULT_TASK_PROVIDER
    if provider not in SUPPORTED_TASK_PROVIDERS:
        known = ", ".join(sorted(SUPPORTED_TASK_PROVIDERS))
        raise ValueError(
            f"Unsupported AWM_TASK_LLM_PROVIDER {provider!r}. Supported: {known}."
        )

    override_model = (os.getenv("AWM_TASK_LLM_MODEL", "") or "").strip()
    if provider == "deepseek":
        # Substitution applies on the default path too: the contracts still name
        # gpt-5.6-luna, which DeepSeek rejects outright.
        return provider, override_model or DEFAULT_DEEPSEEK_TASK_MODEL, ()
    return provider, override_model or model, fallback_models


@dataclass(frozen=True)
class TaskProfile:
    """Model/runtime profile for a one-shot task runtime."""

    stage_name: str
    provider: str
    model: str
    fallback_chain: Tuple[ProviderModel, ...]
    llm_timeout_ms: int
    temperature: Optional[float]
    reasoning_effort: str

    @property
    def chain(self) -> Tuple[ProviderModel, ...]:
        return ((self.provider, self.model),) + self.fallback_chain

    def as_dict(self) -> Dict[str, object]:
        return {
            "primary_provider": self.provider,
            "primary_model": self.model,
            "fallbacks": list(self.fallback_chain),
            "chain": list(self.chain),
            "llm_timeout_ms": self.llm_timeout_ms,
            "temperature": self.temperature,
            "reasoning_effort": self.reasoning_effort,
        }


@dataclass(frozen=True)
class TaskDefinition:
    name: str
    description: str
    module: str
    entrypoint: str
    allowed_callers: Tuple[str, ...]
    instructions: str
    instructions_path: str
    side_effect_level: str
    output_contract: str
    can_run_background: bool
    idempotent: bool
    requires_user_visible_progress: bool
    max_runtime_seconds: int
    default_profile: TaskProfile


def _validate_fields(path: Path, metadata: Mapping[str, Any]) -> None:
    missing = sorted(TASK_REQUIRED_FIELDS - set(metadata))
    unknown = sorted(set(metadata) - TASK_REQUIRED_FIELDS - TASK_OPTIONAL_FIELDS)
    if missing:
        raise ValueError(f"{path}: missing required frontmatter fields {missing}")
    if unknown:
        raise ValueError(f"{path}: unknown frontmatter fields {unknown}")


def _string(value: Any, *, path: Path, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: {field} must be a non-empty string")
    return value.strip()


def _string_tuple(value: Any, *, path: Path, field: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{path}: {field} must be a list of non-empty strings")
    result = tuple(dict.fromkeys(item.strip() for item in value))
    if len(result) != len(value):
        raise ValueError(f"{path}: {field} must not contain duplicates")
    return result


def _positive_number(value: Any, *, path: Path, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{path}: {field} must be a positive number")
    return float(value)


def _temperature(value: Any, *, path: Path) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path}: temperature must be a number or null")
    if not 0 <= float(value) <= 2:
        raise ValueError(f"{path}: temperature must be between 0 and 2")
    return float(value)


def _definition_from_contract(path: Path) -> TaskDefinition:
    metadata, instructions = read_frontmatter(path)
    _validate_fields(path, metadata)

    key = _string(metadata["key"], path=path, field="key")
    if path.parent.name != key:
        raise ValueError(f"{path}: key must match task directory name {path.parent.name!r}")

    reasoning_effort = _string(
        metadata["reasoning_effort"], path=path, field="reasoning_effort"
    ).lower()
    if reasoning_effort not in VALID_REASONING_EFFORTS:
        raise ValueError(
            f"{path}: reasoning_effort must be one of {sorted(VALID_REASONING_EFFORTS)}"
        )

    execution = _string(metadata["execution"], path=path, field="execution").lower()
    if execution not in VALID_EXECUTION_MODES:
        raise ValueError(f"{path}: execution must be one of {sorted(VALID_EXECUTION_MODES)}")

    side_effect_level = _string(
        metadata["side_effect_level"], path=path, field="side_effect_level"
    ).lower()
    if side_effect_level not in VALID_SIDE_EFFECT_LEVELS:
        raise ValueError(
            f"{path}: side_effect_level must be one of {sorted(VALID_SIDE_EFFECT_LEVELS)}"
        )

    idempotent = metadata["idempotent"]
    if not isinstance(idempotent, bool):
        raise ValueError(f"{path}: idempotent must be a boolean")
    requires_progress = metadata.get("requires_user_visible_progress", False)
    if not isinstance(requires_progress, bool):
        raise ValueError(f"{path}: requires_user_visible_progress must be a boolean")

    fallback_models = _string_tuple(
        metadata["fallback_models"], path=path, field="fallback_models"
    )
    timeout_seconds = _positive_number(
        metadata["timeout_seconds"], path=path, field="timeout_seconds"
    )
    max_runtime_seconds = _positive_number(
        metadata["max_runtime_seconds"], path=path, field="max_runtime_seconds"
    )
    model = _string(metadata["default_model"], path=path, field="default_model")
    provider, model, fallback_models = _apply_provider_override(model, fallback_models)

    return TaskDefinition(
        name=key,
        description=_string(metadata["description"], path=path, field="description"),
        module=_string(metadata["module"], path=path, field="module"),
        entrypoint=_string(metadata["entrypoint"], path=path, field="entrypoint"),
        allowed_callers=_string_tuple(
            metadata["allowed_callers"], path=path, field="allowed_callers"
        ),
        instructions=instructions,
        instructions_path=str(path),
        side_effect_level=side_effect_level,
        output_contract=_string(
            metadata.get("output_contract", "dict"), path=path, field="output_contract"
        ),
        can_run_background=execution == "background",
        idempotent=idempotent,
        requires_user_visible_progress=requires_progress,
        max_runtime_seconds=int(max_runtime_seconds),
        default_profile=TaskProfile(
            stage_name=key,
            provider=provider,
            model=model,
            fallback_chain=tuple((provider, item) for item in fallback_models),
            llm_timeout_ms=int(timeout_seconds * 1000),
            temperature=_temperature(metadata["temperature"], path=path),
            reasoning_effort=reasoning_effort,
        ),
    )


def load_task_definitions(
    paths: Iterable[Path] | None = None,
) -> Dict[str, TaskDefinition]:
    resolved_paths = list(paths) if paths is not None else sorted(
        TASK_INSTRUCTION_ROOT.glob("*/task_contract.txt")
    )
    definitions: Dict[str, TaskDefinition] = {}
    for path in resolved_paths:
        definition = _definition_from_contract(path)
        if definition.name in definitions:
            raise ValueError(f"{path}: duplicate task key {definition.name!r}")
        definitions[definition.name] = definition
    return definitions


TASK_RUNTIME_REGISTRY: Dict[str, TaskDefinition] = load_task_definitions()


def get_task_definition(name: str) -> TaskDefinition:
    try:
        return TASK_RUNTIME_REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"Unknown task runtime: {name!r}") from exc


def get_task_profile(name: str) -> TaskProfile:
    profile = get_task_definition(name).default_profile
    model_override = os.getenv(f"AWM_TASK_{name.upper()}_MODEL", "").strip()
    return replace(profile, model=model_override) if model_override else profile


def list_task_definitions() -> Dict[str, TaskDefinition]:
    return dict(TASK_RUNTIME_REGISTRY)


def list_task_profiles() -> Dict[str, TaskProfile]:
    return {name: get_task_profile(name) for name in TASK_RUNTIME_REGISTRY}

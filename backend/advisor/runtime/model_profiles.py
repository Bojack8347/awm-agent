"""Central model/provider profiles for AWM runtime stages.

Cloud Run should normally select one profile with ``AWM_MODEL_PROFILE``.
Per-stage env vars still work as emergency overrides, but this module is the
reviewable source of truth for the standard model map.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Mapping, Tuple

ProviderModel = Tuple[str, str]
EnvGetter = Callable[[str, str], str]

DEFAULT_MODEL_PROFILE = "deepseek_current"
_DEEPSEEK_FLASH = "deepseek-v4-flash"


@dataclass(frozen=True)
class ModelProfileChoice:
    provider: str
    model: str
    fallback_chain: Tuple[ProviderModel, ...] = ()
    from_profile: bool = False


_STAGES = (
    "companion_reasoning_reformulation",
    "financial_planning",
    "financial_planning_synthesis",
    "diagnosis_tool_loop",
    "diagnosis_synthesis",
    "solution_tool_loop",
    "solution_synthesis",
)

# DeepSeek serves an OpenAI-compatible Chat Completions surface. These models
# reason natively with no effort knob, so stages keep reasoning_effort unset
# rather than mapping it (see llm.model_capabilities).
_DEEPSEEK_FLASH_CHOICE = ModelProfileChoice("deepseek", _DEEPSEEK_FLASH, from_profile=True)
_DEEPSEEK_CURRENT: Dict[str, ModelProfileChoice] = {
    stage: _DEEPSEEK_FLASH_CHOICE for stage in _STAGES
}

MODEL_PROFILES: Mapping[str, Mapping[str, ModelProfileChoice]] = {
    "deepseek_current": _DEEPSEEK_CURRENT,
    "deepseek": _DEEPSEEK_CURRENT,
}


def active_model_profile_name(env_getter: EnvGetter) -> str:
    """Return the selected profile name, defaulting to the DeepSeek profile."""
    return (env_getter("AWM_MODEL_PROFILE", "") or DEFAULT_MODEL_PROFILE).strip().lower()


def model_choice_for_stage(
    stage_name: str,
    *,
    env_getter: EnvGetter,
    default_provider: str,
    default_model: str,
) -> ModelProfileChoice:
    """Resolve the profile default for a stage.

    Unknown profile names fail during startup instead of silently falling back
    to an unintended provider. Missing stage entries fall back to the caller's
    explicit default, which preserves compatibility for future/experimental
    stages while keeping known stages centralized here.
    """
    profile_name = active_model_profile_name(env_getter)
    try:
        profile = MODEL_PROFILES[profile_name]
    except KeyError as exc:
        known = ", ".join(sorted(MODEL_PROFILES))
        raise ValueError(
            f"Unknown AWM_MODEL_PROFILE={profile_name!r}; expected one of: {known}"
        ) from exc

    choice = profile.get(stage_name)
    if choice is not None:
        return choice
    return ModelProfileChoice(default_provider, default_model, from_profile=False)

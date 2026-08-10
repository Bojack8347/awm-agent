"""Model capability helpers for provider request shaping."""

from __future__ import annotations


def model_supports_reasoning_effort(model: str | None) -> bool:
    """Return True when the target model accepts an explicit reasoning effort.

    DeepSeek's V4 models reason natively and expose no effort knob — they
    return ``reasoning_content`` regardless and reject nothing, so sending an
    effort would be inventing a control that does not exist. No currently
    supported model takes one, so this is always False; it stays as a named
    seam so a future provider with a real effort parameter has one place to
    opt in.
    """
    return False


def provider_supports_strict_json_schema(provider: str | None) -> bool:
    """Return True when the provider enforces a strict JSON-schema response.

    DeepSeek rejects strict ``json_schema`` with HTTP 400 ("This
    response_format type is unavailable now") and only offers ``json_object``,
    which constrains syntax but not shape. AWM therefore validates structured
    output itself — see ``advisor.llm.schema_validation``. No supported
    provider enforces it server-side today.
    """
    return False


def effective_reasoning_effort(model: str | None, effort: str | None) -> str | None:
    """Normalize effort for the target model; drop unsupported / none values.

    Task and agent contracts still declare a per-stage effort, so this stays
    the single place that decides whether it reaches the provider.
    """
    value = str(effort or "").strip().lower()
    if not value or value == "none":
        return None
    if not model_supports_reasoning_effort(model):
        return None
    return value

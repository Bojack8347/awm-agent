"""Provider-independent enforcement of structured-output schemas.

ARC's provider does not enforce ``response_schema`` server-side: DeepSeek
rejects strict ``json_schema`` with HTTP 400 and only offers ``json_object``,
which constrains the reply to *some* JSON, not to the requested shape.

Without this module the model's reply reaches callers unchecked, and three of
those callers (``client_profile``, ``knowledge_updater``,
``activation_mutation``) write into the Client File. This module moves the
guarantee into ARC so it holds regardless of provider.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover
    Draft202012Validator = None  # type: ignore[assignment]


class LLMSchemaValidationError(ValueError):
    """Raised when a structured response does not satisfy its schema.

    Subclasses ``ValueError`` so the existing provider-fallback loops in the
    task agents treat it as a failed attempt and move to the next candidate
    rather than surfacing malformed data to the caller.
    """


def extract_json_payload(text: str) -> Any:
    """Parse a JSON payload from a model reply.

    Mirrors the leniency the task agents already apply (bare parse, then
    outermost-brace extraction) so enforcement does not reject replies that
    would previously have been salvaged — only ones that are genuinely not the
    requested shape.
    """
    stripped = str(text or "").strip()
    if not stripped:
        raise LLMSchemaValidationError("Structured response was empty.")

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise LLMSchemaValidationError(
        f"Structured response was not valid JSON (first 200 chars: {stripped[:200]!r})."
    )


def validate_response_payload(
    text: str,
    schema: Dict[str, Any],
    *,
    provider: str = "",
    model: str = "",
) -> Any:
    """Parse ``text`` and validate it against ``schema``; return the payload.

    Raises :class:`LLMSchemaValidationError` when the payload is unparseable or
    violates the schema. Reports every violation rather than only the first, so
    a prompt or schema mismatch is diagnosable from one failure.
    """
    payload = extract_json_payload(text)

    if Draft202012Validator is None:  # pragma: no cover
        raise LLMSchemaValidationError(
            "jsonschema is not installed; cannot enforce structured output. "
            "Install it rather than accepting unvalidated model output."
        )

    errors = sorted(
        Draft202012Validator(schema).iter_errors(payload),
        key=lambda err: list(err.absolute_path),
    )
    if not errors:
        return payload

    origin = f"{provider}/{model}".strip("/")
    details = "; ".join(
        f"{'/'.join(str(part) for part in err.absolute_path) or '<root>'}: {err.message}"
        for err in errors[:10]
    )
    suffix = f" (+{len(errors) - 10} more)" if len(errors) > 10 else ""
    raise LLMSchemaValidationError(
        f"Structured response from {origin or 'model'} violates its schema: {details}{suffix}"
    )


def enforce_schema(
    text: str,
    schema: Optional[Dict[str, Any]],
    *,
    has_tools: bool = False,
    provider: str = "",
    model: str = "",
) -> None:
    """Enforce ``schema`` when one applies to this response.

    No-ops when no schema was requested, or when the request carried tools —
    tool-use requests intentionally ignore ``response_schema`` (the reply is a
    tool call, not a structured document).
    """
    if not schema or has_tools:
        return
    validate_response_payload(text, schema, provider=provider, model=model)

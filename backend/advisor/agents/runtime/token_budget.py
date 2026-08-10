"""Token accounting for the budgeted conversation context."""

from __future__ import annotations

import math
import os
from functools import lru_cache
from typing import Any, Dict, Iterable, List


DEFAULT_CHAT_RECORD_TOKEN_BUDGET = 60_000
DEFAULT_CHAT_RECORD_SOFT_PCT = 0.75
DEFAULT_CHAT_RECORD_COMPACT_FRACTION = 0.50
DEFAULT_SUMMARY_RECOMPACT_PCT = 0.50
DEFAULT_COMPACT_SAFETY_GAP = 10


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _fraction_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if 0 < value <= 1 else default


def chat_record_token_budget() -> int:
    return _positive_int_env(
        "AWM_CHAT_RECORD_TOKEN_BUDGET",
        DEFAULT_CHAT_RECORD_TOKEN_BUDGET,
    )


def chat_record_soft_pct() -> float:
    return _fraction_env("AWM_CHAT_RECORD_SOFT_PCT", DEFAULT_CHAT_RECORD_SOFT_PCT)


def chat_record_compact_fraction() -> float:
    return _fraction_env(
        "AWM_CHAT_RECORD_COMPACT_FRACTION",
        DEFAULT_CHAT_RECORD_COMPACT_FRACTION,
    )


def summary_recompact_pct() -> float:
    return _fraction_env("AWM_SUMMARY_RECOMPACT_PCT", DEFAULT_SUMMARY_RECOMPACT_PCT)


def compact_safety_gap() -> int:
    return _positive_int_env("AWM_COMPACT_SAFETY_GAP", DEFAULT_COMPACT_SAFETY_GAP)


def conversation_memory_enabled() -> bool:
    return os.getenv("AWM_CONVERSATION_MEMORY", "on").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


@lru_cache(maxsize=8)
def _encoding_for_model(model: str):
    import tiktoken

    return tiktoken.encoding_for_model(model)


def _fallback_token_count(text: str) -> int:
    return int(math.ceil(len(text) / 3.5)) if text else 0


def count_text_tokens(text: str, *, model: str = "gpt-4.1") -> int:
    """Count text tokens, conservatively falling back when encoding is unavailable."""
    value = str(text or "")
    try:
        return len(_encoding_for_model(model).encode(value))
    except Exception:
        return _fallback_token_count(value)


def count_message_tokens(message: Dict[str, Any], *, model: str = "gpt-4.1") -> int:
    """Count one role/content input item including message framing."""
    # OpenAI chat-style message framing is approximately three tokens per item.
    return (
        3
        + count_text_tokens(str(message.get("role") or ""), model=model)
        + count_text_tokens(str(message.get("content") or ""), model=model)
    )


def chat_record_items(
    messages: Iterable[Dict[str, Any]],
    *,
    current_user_message: str,
    summary_block: str = "",
) -> List[Dict[str, str]]:
    """Return budgeted items with the already-persisted current turn counted once."""
    items: List[Dict[str, str]] = []
    if summary_block:
        items.append({"role": "developer", "content": str(summary_block)})

    normalized = [
        {
            "role": str(message.get("role") or "").strip().lower(),
            "content": str(message.get("content") or "").strip(),
        }
        for message in messages
        if isinstance(message, dict)
        and str(message.get("role") or "").strip().lower() in {"user", "assistant"}
        and str(message.get("content") or "").strip()
    ]
    current = str(current_user_message or "").strip()
    if (
        normalized
        and current
        and normalized[-1]["role"] == "user"
        and normalized[-1]["content"] == current
    ):
        normalized.pop()
    items.extend(normalized)
    if current:
        items.append({"role": "user", "content": current})
    return items


def count_chat_record_tokens(
    messages: Iterable[Dict[str, Any]],
    *,
    current_user_message: str,
    summary_block: str = "",
    model: str = "gpt-4.1",
) -> int:
    """Count summary, raw history, and current user tokens exactly once."""
    items = chat_record_items(
        messages,
        current_user_message=current_user_message,
        summary_block=summary_block,
    )
    if not items:
        return 0
    return sum(count_message_tokens(item, model=model) for item in items) + 3

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

from dotenv import dotenv_values

from advisor.agents.catalog import MAIN_ADVISOR
from advisor.agents.context import AwmAgentContext


def _ensure_openai_proxy_env() -> None:
    """Expose OPENAI_PROXY_URL to SDK clients that only honor standard proxy envs."""

    if os.getenv("HTTPS_PROXY") or os.getenv("https_proxy"):
        return
    proxy_url = str(os.getenv("OPENAI_PROXY_URL") or "").strip()
    if not proxy_url:
        repo_root = Path(__file__).resolve().parents[3]
        for env_path in (repo_root / ".env", repo_root / ".evn"):
            if env_path.exists():
                proxy_url = str((dotenv_values(env_path).get("OPENAI_PROXY_URL") or "")).strip()
                if proxy_url:
                    break
    if not proxy_url:
        return
    os.environ.setdefault("HTTPS_PROXY", proxy_url)
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")


def _agent_run_timeout_seconds() -> float:
    raw = str(os.getenv("AWM_AGENT_RUN_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return MAIN_ADVISOR.resolved_timeout_seconds
    try:
        value = float(raw)
    except ValueError:
        return MAIN_ADVISOR.resolved_timeout_seconds
    return value if value > 0 else MAIN_ADVISOR.resolved_timeout_seconds


def _sdk_error_output(exc: Exception) -> Dict[str, str]:
    return {"error": type(exc).__name__, "detail": str(exc)}


def _should_retry_sdk_run(exc: Exception, context: AwmAgentContext) -> bool:
    # Voice is already a live, user-facing stream. Retrying an otherwise empty
    # SDK run doubles perceived silence and can leave Realtime reading a stale
    # partial response. Let the client retry explicitly instead.
    if str(getattr(context, "channel", "") or "").lower() == "voice":
        return False
    if context.tool_results or context.llm_calls:
        return False
    text = f"{type(exc).__name__}: {exc}".lower()
    retry_markers = (
        "apiconnectionerror",
        "connection error",
        "timeout",
        "timed out",
        "temporarily unavailable",
        "server disconnected",
        "rate limit",
    )
    return any(marker in text for marker in retry_markers)

"""Shared LLM and quant-model endpoint configuration.

This module is intentionally small: it keeps API keys and model URLs used by
background LLM tasks and deterministic financial adapters. It does not contain
an agent loop, tool routing, or ReAct runtime.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from dotenv import dotenv_values


@dataclass
class ToolLoopConfig:
    """Endpoint/API-key plumbing shared by AWM LLM tasks and model adapters."""

    # Credential for the configured LLM provider (DeepSeek).
    llm_api_key: str

    cashflow_model_url: str = "http://localhost:8001"
    cashflow_api_key: str = ""

    asset_allocation_model_url: str = "http://localhost:8600"
    asset_allocation_model_api_key: str = ""
    asset_allocation_model_optimize_path: str = "/asset-allocation/api/v1/optimize"

    @classmethod
    def from_env(cls) -> "ToolLoopConfig":
        """Build configuration from environment variables and the repo root env."""
        repo_root = Path(__file__).resolve().parents[3]
        root_env_path = repo_root / ".env"
        root_evn_path = repo_root / ".evn"
        root_env: Dict[str, str] = {}
        source_env_path = root_env_path if root_env_path.exists() else root_evn_path
        if source_env_path.exists():
            parsed = dotenv_values(source_env_path)
            root_env = {str(k): str(v or "") for k, v in parsed.items()}

        def _env(name: str) -> str:
            return os.getenv(name, "").strip() or root_env.get(name, "").strip()

        llm_key = _env("DEEPSEEK_API_KEY") or _env("AWM_DEEPSEEK_API_KEY")

        asset_allocation_model_url = _env("ASSET_ALLOCATION_MODEL_URL") or "http://localhost:8600"
        explicit_asset_allocation_model_api_key = _env("ASSET_ALLOCATION_MODEL_API_KEY")
        asset_allocation_model_api_secret = _env("ASSET_ALLOCATION_MODEL_API_SECRET") or _env("API_SECRET")
        if (
            not asset_allocation_model_api_secret
            and not explicit_asset_allocation_model_api_key
            and (
                asset_allocation_model_url.startswith("http://localhost:")
                or asset_allocation_model_url.startswith("http://127.0.0.1:")
            )
        ):
            asset_allocation_model_api_secret = "local-dev-secret"

        if asset_allocation_model_api_secret:
            asset_allocation_model_api_key = hmac.new(
                asset_allocation_model_api_secret.encode("utf-8"), b"api_key", hashlib.sha256
            ).hexdigest()
        elif explicit_asset_allocation_model_api_key:
            asset_allocation_model_api_key = explicit_asset_allocation_model_api_key
        else:
            asset_allocation_model_api_key = ""

        cashflow_model_url = _env("CASHFLOW_MODEL_URL") or "http://localhost:8001"

        return cls(
            llm_api_key=llm_key,
            cashflow_model_url=cashflow_model_url.rstrip("/"),
            cashflow_api_key=_env("CASHFLOW_API_KEY"),
            asset_allocation_model_url=asset_allocation_model_url.rstrip("/"),
            asset_allocation_model_api_key=asset_allocation_model_api_key,
            asset_allocation_model_optimize_path=_env("ASSET_ALLOCATION_MODEL_OPTIMIZE_PATH") or "/asset-allocation/api/v1/optimize",
        )

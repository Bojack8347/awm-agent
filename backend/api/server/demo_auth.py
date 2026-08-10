"""Single policy boundary for development-only demo authentication."""

from __future__ import annotations

import os


def is_demo_auth_enabled() -> bool:
    return os.getenv("ENABLE_DEMO_AUTH", "false").strip().lower() in {"1", "true", "yes", "on"}


def validate_demo_auth_environment() -> None:
    environment = os.getenv("AWM_ENV", os.getenv("FLASK_ENV", "")).strip().lower()
    if is_demo_auth_enabled() and environment in {"production", "prod"}:
        raise RuntimeError("ENABLE_DEMO_AUTH must be disabled in production")

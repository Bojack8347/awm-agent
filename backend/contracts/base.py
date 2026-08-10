"""Base helpers for AWM Pydantic contracts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class AwmContractModel(BaseModel):
    """Default contract model: typed, strict about unknown fields."""

    model_config = ConfigDict(extra="forbid")


class AwmFlexibleContractModel(BaseModel):
    """Contract model for intentionally extensible payloads."""

    model_config = ConfigDict(extra="allow")

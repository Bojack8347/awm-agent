"""Contracts for silent sub-agents."""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, ConfigDict, Field


class SubAgentRequest(BaseModel):
    """Request from Main Agent to a silent specialist."""

    model_config = ConfigDict(extra="forbid")

    client_id: str
    objective: Dict[str, Any] = Field(default_factory=dict)
    client_file: Dict[str, Any] = Field(default_factory=dict)


class SubAgentArtifact(BaseModel):
    """Artifact returned by a silent specialist agent."""

    model_config = ConfigDict(extra="forbid")

    artifact_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    writeback_target: str


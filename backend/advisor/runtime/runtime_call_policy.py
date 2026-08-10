"""Runtime call policies for caller -> callee execution edges.

This module separates two concerns that are easy to blur:

- AgentDefinition / TaskDefinition describe what a runtime is capable of.
- RuntimeCallPolicy describes how one specific caller invokes one callee.

JourneyRuntime consumes this contract for journey completion and worker
serviceability checks; Cloud Tasks remains the durable execution engine for
detached journey work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple, Literal


ExecutionMode = Literal["inline", "cloud_task"]
WaitPolicy = Literal[
    "await_result",
    "poll_state",
    "chat_followup",
    "fire_and_forget",
]
ResultStore = Literal[
    "return_value",
    "journey_run",
    "diagnosis_snapshot",
    "companion_message",
    "none",
]


def policy_key(caller: str, callee: str) -> str:
    """Stable key used by registries and journey overrides."""
    return f"{caller}->{callee}"


@dataclass(frozen=True)
class RuntimeCallPolicy:
    """Execution behavior for one caller -> callee relationship."""

    caller: str
    callee: str
    execution_mode: ExecutionMode
    wait_policy: WaitPolicy
    result_store: ResultStore
    description: str = ""
    retryable: bool = False
    max_runtime_seconds: Optional[int] = None

    def __post_init__(self) -> None:
        """Reject impossible execution contracts at definition time."""
        if self.execution_mode == "cloud_task":
            if self.wait_policy == "await_result":
                raise ValueError("cloud_task policies cannot await an immediate result")
            if self.result_store == "return_value":
                raise ValueError("cloud_task policies need a durable result_store")
        if self.wait_policy == "fire_and_forget" and self.result_store == "return_value":
            raise ValueError("fire_and_forget policies cannot use return_value")

    @property
    def key(self) -> str:
        return policy_key(self.caller, self.callee)

    @property
    def is_detached(self) -> bool:
        return self.execution_mode == "cloud_task"

    def as_dict(self) -> Dict[str, object]:
        return {
            "caller": self.caller,
            "callee": self.callee,
            "execution_mode": self.execution_mode,
            "wait_policy": self.wait_policy,
            "result_store": self.result_store,
            "description": self.description,
            "retryable": self.retryable,
            "max_runtime_seconds": self.max_runtime_seconds,
        }


DEFAULT_RUNTIME_CALL_POLICIES: Dict[str, RuntimeCallPolicy] = {
    policy_key("companion", "journey.complete"): RuntimeCallPolicy(
        caller="companion",
        callee="journey.complete",
        execution_mode="cloud_task",
        wait_policy="poll_state",
        result_store="journey_run",
        retryable=True,
        max_runtime_seconds=600,
        description=(
            "User-facing journey completion detaches from the companion turn; "
            "mobile/companion reattach by reading journey state."
        ),
    ),
    policy_key("knowledge_service", "diagnosis_refresh"): RuntimeCallPolicy(
        caller="knowledge_service",
        callee="diagnosis_refresh",
        execution_mode="cloud_task",
        wait_policy="fire_and_forget",
        result_store="diagnosis_snapshot",
        retryable=True,
        max_runtime_seconds=300,
        description=(
            "Diagnosis is derived state over committed knowledge snapshots; "
            "knowledge writes should not block on refresh."
        ),
    ),
    policy_key("companion", "financial_planning_ad_hoc"): RuntimeCallPolicy(
        caller="companion",
        callee="financial_planning_ad_hoc",
        execution_mode="cloud_task",
        wait_policy="chat_followup",
        result_store="companion_message",
        retryable=True,
        max_runtime_seconds=180,
        description=(
            "Longer numeric reasoning can answer later in chat instead of "
            "holding the companion response open."
        ),
    ),
}


def list_default_call_policies() -> Dict[str, RuntimeCallPolicy]:
    return dict(DEFAULT_RUNTIME_CALL_POLICIES)


def resolve_call_policy(
    caller: str,
    callee: str,
    *,
    journey_policies: Optional[Mapping[str, RuntimeCallPolicy]] = None,
) -> Optional[RuntimeCallPolicy]:
    """Resolve a policy, preferring journey-specific overrides.

    `journey_policies` is usually `JourneyDefinition.call_policies`. A journey
    may override a global default or declare product-specific worker steps.
    """
    key = policy_key(caller, callee)
    if journey_policies and key in journey_policies:
        return journey_policies[key]
    return DEFAULT_RUNTIME_CALL_POLICIES.get(key)


def policies_by_edge(
    policies: Tuple[RuntimeCallPolicy, ...],
) -> Dict[str, RuntimeCallPolicy]:
    """Convert a compact tuple declaration into a keyed registry."""
    return {policy.key: policy for policy in policies}

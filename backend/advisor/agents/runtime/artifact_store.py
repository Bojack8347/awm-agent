from __future__ import annotations

from typing import Any, Dict, List

from advisor.agents.context import AwmAgentContext
from advisor.agents.runtime._shared import _subagent_artifact_key
from advisor.agents.runtime.solution_artifacts import (
    _investment_solution_artifact_from_allocation_writeback,
)


def _extend_subagent_artifacts(context: AwmAgentContext, artifacts: List[Dict[str, Any]]) -> None:
    if not artifacts:
        return
    seen = {_subagent_artifact_key(artifact) for artifact in context.subagent_artifacts}
    for artifact in artifacts:
        key = _subagent_artifact_key(artifact)
        if key in seen:
            continue
        seen.add(key)
        context.subagent_artifacts.append(artifact)


def _combined_subagent_artifacts(context: AwmAgentContext) -> List[Dict[str, Any]]:
    artifacts = list(context.subagent_artifacts)
    artifacts.extend(_subagent_artifacts_from_tool_results(context.tool_results, context.client_file))
    return _dedupe_subagent_artifacts(artifacts)


def _dedupe_subagent_artifacts(artifacts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for artifact in artifacts:
        key = _subagent_artifact_key(artifact)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(artifact)
    return deduped


def _subagent_artifacts_from_tool_results(
    tool_results: List[Dict[str, Any]],
    client_file: Dict[str, Any],
) -> List[Dict[str, Any]]:
    artifacts: List[Dict[str, Any]] = []
    for result in tool_results:
        if not isinstance(result, dict):
            continue
        if result.get("tool") != "run_asset_allocation" or result.get("ok") is not True:
            continue
        writeback = result.get("proposal_writeback")
        if not isinstance(writeback, dict):
            continue
        artifact = _investment_solution_artifact_from_allocation_writeback(
            tool_result=result,
            proposal_writeback=writeback,
            client_file=client_file,
        )
        if artifact is not None:
            artifacts.append(artifact)
    return artifacts

"""Fail-fast YAML frontmatter loader for AWM agent and skill declarations."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple

from advisor.instructions import (
    MAIN_AGENT_INSTRUCTION_ROOT,
    SKILL_INSTRUCTION_ROOT,
    SUBAGENT_INSTRUCTION_ROOT,
)
from advisor.instructions.manifest import read_frontmatter, read_instruction_text
from advisor.tools.capabilities import expand_agent_capabilities


AGENT_REQUIRED_FIELDS = {
    "key",
    "name",
    "description",
    "default_model",
    "max_turns",
    "capabilities",
    "execution",
}
AGENT_OPTIONAL_FIELDS = {
    "reasoning_effort",
    "tool_choice",
    "tool_use_behavior",
    "parallel_tool_calls",
    "timeout_seconds",
    "channel_scope",
}
SKILL_REQUIRED_FIELDS = {
    "name",
    "summary",
    "when_to_use",
    "allowed_agents",
    "capabilities",
}
VALID_EXECUTION_MODES = {"wait", "background"}


def _validate_fields(
    path: Path,
    metadata: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str],
) -> None:
    missing = sorted(required - set(metadata))
    unknown = sorted(set(metadata) - required - optional)
    if missing:
        raise ValueError(f"{path}: missing required frontmatter fields {missing}")
    if unknown:
        raise ValueError(f"{path}: unknown frontmatter fields {unknown}")


def _string(value: Any, *, path: Path, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: {field} must be a non-empty string")
    return value.strip()


def _string_tuple(value: Any, *, path: Path, field: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{path}: {field} must be a list of non-empty strings")
    result = tuple(dict.fromkeys(item.strip() for item in value))
    if len(result) != len(value):
        raise ValueError(f"{path}: {field} must not contain duplicates")
    return result


def _agent_paths() -> List[Path]:
    return [
        MAIN_AGENT_INSTRUCTION_ROOT / "agent_contract.txt",
        *sorted(SUBAGENT_INSTRUCTION_ROOT.glob("*/agent_contract.txt")),
    ]


def load_agent_records(
    paths: Iterable[Path] | None = None,
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    seen: set[str] = set()
    resolved_paths = list(paths) if paths is not None else _agent_paths()
    for path in resolved_paths:
        metadata, body = read_frontmatter(path)
        _validate_fields(
            path,
            metadata,
            required=AGENT_REQUIRED_FIELDS,
            optional=AGENT_OPTIONAL_FIELDS,
        )
        key = _string(metadata["key"], path=path, field="key")
        if key in seen:
            raise ValueError(f"{path}: duplicate agent key {key!r}")
        seen.add(key)
        capabilities = _string_tuple(
            metadata["capabilities"],
            path=path,
            field="capabilities",
        )
        execution = _string(metadata["execution"], path=path, field="execution")
        if execution not in VALID_EXECUTION_MODES:
            raise ValueError(
                f"{path}: execution must be one of {sorted(VALID_EXECUTION_MODES)}"
            )
        max_turns = metadata["max_turns"]
        if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns < 1:
            raise ValueError(f"{path}: max_turns must be a positive integer")
        parallel_tool_calls = metadata.get("parallel_tool_calls", False)
        if not isinstance(parallel_tool_calls, bool):
            raise ValueError(f"{path}: parallel_tool_calls must be a boolean")
        timeout_seconds = metadata.get("timeout_seconds")
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError(f"{path}: timeout_seconds must be a positive number")
        system_instructions_path = path.with_name("agent_system.txt")
        system_instructions = read_instruction_text(system_instructions_path)
        records.append(
            {
                **metadata,
                "key": key,
                "name": _string(metadata["name"], path=path, field="name"),
                "description": _string(
                    metadata["description"],
                    path=path,
                    field="description",
                ),
                "default_model": _string(
                    metadata["default_model"],
                    path=path,
                    field="default_model",
                ),
                "max_turns": max_turns,
                "capabilities": capabilities,
                "tool_names": expand_agent_capabilities(capabilities),
                "parallel_tool_calls": parallel_tool_calls,
                "timeout_seconds": (
                    float(timeout_seconds) if timeout_seconds is not None else None
                ),
                "execution": execution,
                "instructions": body,
                "instructions_path": str(path),
                "system_instructions": system_instructions,
                "system_instructions_path": str(system_instructions_path),
            }
        )
    if paths is None and len(records) != 7:
        raise ValueError(
            f"Expected exactly 7 agent declarations, found {len(records)}"
        )
    return records


def load_skill_records(
    paths: Iterable[Path] | None = None,
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    seen: set[str] = set()
    resolved_paths = (
        list(paths)
        if paths is not None
        else sorted(SKILL_INSTRUCTION_ROOT.glob("*/skill.md"))
    )
    for path in resolved_paths:
        metadata, body = read_frontmatter(path)
        _validate_fields(
            path,
            metadata,
            required=SKILL_REQUIRED_FIELDS,
            optional=set(),
        )
        name = _string(metadata["name"], path=path, field="name")
        if name in seen:
            raise ValueError(f"{path}: duplicate skill name {name!r}")
        seen.add(name)
        capabilities = _string_tuple(
            metadata["capabilities"],
            path=path,
            field="capabilities",
        )
        records.append(
            {
                **metadata,
                "name": name,
                "summary": _string(metadata["summary"], path=path, field="summary"),
                "when_to_use": _string(
                    metadata["when_to_use"],
                    path=path,
                    field="when_to_use",
                ),
                "allowed_agents": _string_tuple(
                    metadata["allowed_agents"],
                    path=path,
                    field="allowed_agents",
                ),
                "capabilities": capabilities,
                "tool_names": expand_agent_capabilities(capabilities),
                "instruction_files": (
                    str(path.relative_to(SKILL_INSTRUCTION_ROOT)),
                ),
                "instructions": body,
            }
        )
    if paths is None and len(records) != 15:
        raise ValueError(
            f"Expected exactly 15 skill declarations, found {len(records)}"
        )
    return records

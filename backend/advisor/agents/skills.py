"""Installable AWM skills parsed from authoritative instruction frontmatter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

from advisor.agents.manifest_loader import load_skill_records


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    summary: str
    when_to_use: str
    instruction_files: Tuple[str, ...]
    allowed_agents: Tuple[str, ...]
    capabilities: Tuple[str, ...]
    tool_names: Tuple[str, ...]
    instructions: str


class SkillRegistry:
    def __init__(self, skills: Iterable[SkillDefinition]) -> None:
        self._skills: Dict[str, SkillDefinition] = {
            skill.name: skill
            for skill in skills
        }

    def get(self, name: str) -> SkillDefinition:
        return self._skills[name]

    def list(self) -> List[SkillDefinition]:
        return list(self._skills.values())

    def for_agent(self, agent_key: str) -> List[SkillDefinition]:
        return [
            skill
            for skill in self._skills.values()
            if agent_key in skill.allowed_agents
        ]


def build_default_skill_registry() -> SkillRegistry:
    return SkillRegistry(
        SkillDefinition(
            name=record["name"],
            summary=record["summary"],
            when_to_use=record["when_to_use"],
            instruction_files=record["instruction_files"],
            allowed_agents=record["allowed_agents"],
            capabilities=record["capabilities"],
            tool_names=record["tool_names"],
            instructions=record["instructions"],
        )
        for record in load_skill_records()
    )


DEFAULT_SKILL_REGISTRY = build_default_skill_registry()

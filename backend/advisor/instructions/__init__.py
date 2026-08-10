"""Filesystem locations for active advisor instruction assets."""

from __future__ import annotations

from pathlib import Path


INSTRUCTION_ROOT = Path(__file__).resolve().parent
AGENT_INSTRUCTION_ROOT = INSTRUCTION_ROOT / "agents"
MAIN_AGENT_INSTRUCTION_ROOT = AGENT_INSTRUCTION_ROOT / "main_agent"
SUBAGENT_INSTRUCTION_ROOT = AGENT_INSTRUCTION_ROOT / "subagents"
SKILL_INSTRUCTION_ROOT = INSTRUCTION_ROOT / "skills"
TASK_INSTRUCTION_ROOT = INSTRUCTION_ROOT / "tasks"


def task_prompt_dir(task_name: str) -> Path:
    return TASK_INSTRUCTION_ROOT / task_name

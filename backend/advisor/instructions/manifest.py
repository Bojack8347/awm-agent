"""Shared readers for instruction contracts and prompt files."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

import yaml


def read_frontmatter(path: Path) -> Tuple[Dict[str, Any], str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Unable to read declaration file {path}: {exc}") from exc
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{path}: YAML frontmatter must start on the first line")
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration as exc:
        raise ValueError(f"{path}: YAML frontmatter is missing its closing fence") from exc
    try:
        metadata = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ValueError(f"{path}: frontmatter must be a YAML mapping")
    body = "\n".join(lines[end + 1 :]).strip()
    if not body:
        raise ValueError(f"{path}: instruction prose must not be empty")
    return dict(metadata), body


def read_instruction_text(path: Path) -> str:
    try:
        body = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"Unable to read instruction file {path}: {exc}") from exc
    if not body:
        raise ValueError(f"{path}: instruction text must not be empty")
    return body

from __future__ import annotations

import math
from typing import Any, List, Optional


def _numeric_leaves(value: Any, path: str = "$") -> List[tuple[str, float]]:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return [(path, float(value))]
    output: List[tuple[str, float]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            output.extend(_numeric_leaves(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            output.extend(_numeric_leaves(child, f"{path}[{index}]"))
    return output


def _finite_numeric(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _normalize_visible_text(value: Any) -> str:
    return " ".join(str(value or "").lower().split())

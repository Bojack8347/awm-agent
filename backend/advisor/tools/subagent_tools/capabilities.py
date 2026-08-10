"""Derived capability catalog for specialist-dispatch tools."""

from __future__ import annotations

from typing import Dict, Tuple

from advisor.tools.subagent_tools.assessment_revalidation_specialist.tool import (
    CAPABILITY as ASSESSMENT_REVALIDATION_CAPABILITY,
    TOOL_NAME as ASSESSMENT_REVALIDATION_TOOL_NAME,
)
from advisor.tools.subagent_tools.diagnosis_specialist.tool import (
    CAPABILITY as DIAGNOSIS_CAPABILITY,
    TOOL_NAME as DIAGNOSIS_TOOL_NAME,
)
from advisor.tools.subagent_tools.financial_planning_specialist.tool import (
    CAPABILITY as FINANCIAL_PLANNING_CAPABILITY,
    TOOL_NAME as FINANCIAL_PLANNING_TOOL_NAME,
)
from advisor.tools.subagent_tools.investment_solution_specialist.tool import (
    CAPABILITY as INVESTMENT_SOLUTION_CAPABILITY,
    TOOL_NAME as INVESTMENT_SOLUTION_TOOL_NAME,
)
from advisor.tools.subagent_tools.onboarding_specialist.tool import (
    CAPABILITY as ONBOARDING_CAPABILITY,
    TOOL_NAME as ONBOARDING_TOOL_NAME,
)
from advisor.tools.subagent_tools.policy_review_specialist.tool import (
    CAPABILITY as POLICY_REVIEW_CAPABILITY,
    TOOL_NAME as POLICY_REVIEW_TOOL_NAME,
)


_DECLARATIONS = (
    (ASSESSMENT_REVALIDATION_CAPABILITY, ASSESSMENT_REVALIDATION_TOOL_NAME),
    (DIAGNOSIS_CAPABILITY, DIAGNOSIS_TOOL_NAME),
    (FINANCIAL_PLANNING_CAPABILITY, FINANCIAL_PLANNING_TOOL_NAME),
    (INVESTMENT_SOLUTION_CAPABILITY, INVESTMENT_SOLUTION_TOOL_NAME),
    (ONBOARDING_CAPABILITY, ONBOARDING_TOOL_NAME),
    (POLICY_REVIEW_CAPABILITY, POLICY_REVIEW_TOOL_NAME),
)

SUBAGENT_CAPABILITY_CATALOG: Dict[str, Tuple[str, ...]] = {
    capability: (tool_name,)
    for capability, tool_name in sorted(_DECLARATIONS)
}

if len(SUBAGENT_CAPABILITY_CATALOG) != len(_DECLARATIONS):
    raise ValueError("Each specialist-dispatch tool must have a distinct capability")


def subagent_capabilities_by_name() -> Dict[str, Tuple[str, ...]]:
    return dict(SUBAGENT_CAPABILITY_CATALOG)

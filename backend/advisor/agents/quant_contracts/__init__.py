"""Strict quantitative tool contracts and client-facing evidence validation.

The language model may choose a quantitative capability and narrate its output,
but it is not the authority for either the tool contract or the conclusion.  This
module therefore owns three agent-layer controls:

* strict, versioned SDK input schemas for the two recommendation-grade tools;
* a typed evidence envelope derived only from deterministic tool results; and
* a final-response validator that fails closed on invalid or unsupported claims.

The validator is intentionally conservative.  It is a containment boundary while
the underlying cash-flow and allocation contracts are brought through the full
production acceptance gates documented in the architecture evaluation.
"""

from __future__ import annotations

from advisor.agents.quant_contracts.constants import (
    QUANT_TOOL_NAMES,
    STRICT_QUANT_TOOL_NAMES,
    REQUIRED_ALLOCATION_CONSTRAINT_CHECKS,
)
from advisor.agents.quant_contracts.tool_requests import (
    CashflowScenarioChangeContract,
    CashflowScenarioToolContract,
    CashflowAgentToolRequest,
    SignedAssessmentReferenceContract,
    AssetAllocationAgentToolRequest,
    CashflowContributionSolverRequest,
)
from advisor.agents.quant_contracts.normalization import (
    QuantToolArgumentError,
    normalize_quant_tool_arguments,
    mortgage_default_fallback_authorized,
)
from advisor.agents.quant_contracts.models import (
    QuantEvidenceClaim,
    QuantEvidenceEnvelope,
    QuantValidationIssue,
    QuantConclusionValidation,
    QuantResponseAnnotations,
)
from advisor.agents.quant_contracts.policy import (
    quant_recommendations_enabled,
    quant_recommendation_policy,
)
from advisor.agents.quant_contracts.evidence import (
    build_quant_evidence,
)
from advisor.agents.quant_contracts.response_format import (
    build_quant_response_annotations,
    attach_quant_evidence,
    visible_quant_warnings,
    visible_quant_assumptions,
    propagate_quant_warnings,
    ensure_required_allocation_proposal_metrics,
    format_quant_response_for_client,
)
from advisor.agents.quant_contracts.fallbacks import (
    render_quantitative_reporting_fallback,
    render_quantitative_missing_data_fallback,
    render_asset_allocation_failure_fallback,
)
from advisor.agents.quant_contracts.validation import (
    validate_quantitative_response,
)

__all__ = [
    "QUANT_TOOL_NAMES",
    "STRICT_QUANT_TOOL_NAMES",
    "REQUIRED_ALLOCATION_CONSTRAINT_CHECKS",
    "CashflowScenarioChangeContract",
    "CashflowScenarioToolContract",
    "CashflowAgentToolRequest",
    "SignedAssessmentReferenceContract",
    "AssetAllocationAgentToolRequest",
    "CashflowContributionSolverRequest",
    "QuantToolArgumentError",
    "normalize_quant_tool_arguments",
    "mortgage_default_fallback_authorized",
    "QuantEvidenceClaim",
    "QuantEvidenceEnvelope",
    "QuantValidationIssue",
    "QuantConclusionValidation",
    "QuantResponseAnnotations",
    "quant_recommendations_enabled",
    "quant_recommendation_policy",
    "build_quant_evidence",
    "build_quant_response_annotations",
    "attach_quant_evidence",
    "visible_quant_warnings",
    "visible_quant_assumptions",
    "propagate_quant_warnings",
    "ensure_required_allocation_proposal_metrics",
    "format_quant_response_for_client",
    "render_quantitative_reporting_fallback",
    "render_quantitative_missing_data_fallback",
    "render_asset_allocation_failure_fallback",
    "validate_quantitative_response",
]

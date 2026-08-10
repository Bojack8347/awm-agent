"""Policy activation pipeline.

Chain: policy output -> derived mutations -> validated mutations -> committed truth -> diagnosis

Expected context keys (input):
    journey_id, journey_type, client_id, solution_output, current_facts

Produced context keys (output):
    derive_result, activation_facts, update_result, truth_result, diagnosis_version
"""

from __future__ import annotations

from typing import Any, Dict

from advisor.runtime.pipeline import Pipeline, PipelineStep
from domain.knowledge.truth import commit_truth_update

try:
    from api.persistence import (
        get_knowledge_facts as db_get_knowledge_facts,
    )
except ImportError:
    db_get_knowledge_facts = lambda *a, **kw: []


# ---------------------------------------------------------------------------
# Step functions
# ---------------------------------------------------------------------------

def _derive_and_validate(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """LLM-derive mutations from policy output, then validate deterministically."""
    from api.server import get_activation_mutator

    mutator = get_activation_mutator()
    ctx["derive_result"] = mutator.derive_and_validate(
        ctx["journey_id"],
        ctx["journey_type"],
        ctx["solution_output"],
        ctx["current_facts"],
    )
    ctx["activation_facts"] = ctx["derive_result"].get("mutations", [])
    return ctx


def _commit_mutations(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Commit validated mutations through KnowledgeUpdater and persist."""
    from api.server import get_knowledge_updater

    activation_facts = ctx["activation_facts"]
    if not activation_facts:
        ctx["truth_result"] = None
        return ctx

    updater = get_knowledge_updater()
    ctx["update_result"] = updater.update_knowledge(
        client_id=ctx["client_id"],
        current_facts=ctx["current_facts"],
        candidate_updates=activation_facts,
        evidence_refs=[{"journey_id": ctx["journey_id"], "type": "policy_activation"}],
        source_event_id=ctx["journey_id"],
        trigger_event="policy_activated",
    )

    ctx["truth_result"] = commit_truth_update(
        client_id=ctx["client_id"],
        update_result=ctx["update_result"],
        trigger_event="policy_activated",
        trigger_event_id=ctx["journey_id"],
    )
    return ctx


def _refresh_diagnosis(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Queue diagnosis refresh from updated truth."""
    from api.server import get_diagnosis_service

    refresh_result = get_diagnosis_service().queue_refresh_if_material(
        client_id=ctx["client_id"],
        committed_facts=ctx["activation_facts"],
        caller="activation_pipeline",
    )
    ctx["diagnosis_version"] = None
    ctx["diagnosis_status"] = refresh_result.get("status")
    ctx["diagnosis_refresh_queued"] = refresh_result.get("queued", False)
    return ctx


# ---------------------------------------------------------------------------
# Pipeline definition
# ---------------------------------------------------------------------------

ACTIVATION_PIPELINE = Pipeline("activation", [
    PipelineStep("derive_and_validate", _derive_and_validate),
    PipelineStep(
        "commit_mutations",
        _commit_mutations,
        condition=lambda ctx: bool(ctx.get("activation_facts")),
    ),
    PipelineStep(
        "refresh_diagnosis",
        _refresh_diagnosis,
        condition=lambda ctx: bool(ctx.get("activation_facts")),
        critical=False,
    ),
])

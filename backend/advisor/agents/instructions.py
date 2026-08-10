"""Instruction assembly for AWM's OpenAI Agents SDK agents."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Optional

from advisor.agents.catalog import AgentDefinition, MAIN_ADVISOR
from advisor.agents.skills import DEFAULT_SKILL_REGISTRY, SkillDefinition


def build_main_instructions(
    client_file: Dict[str, Any],
    *,
    active_skill_name: Optional[str] = None,
    skill_candidates: Optional[Dict[str, list[Dict[str, Any]]]] = None,
    artifact_context: Optional[Dict[str, Any]] = None,
    trusted_action_context: Optional[Dict[str, Any]] = None,
    background_jobs: Optional[list[Dict[str, Any]]] = None,
) -> str:
    """Combine the advisor contract, installed skill summaries, active skill, and Client File."""

    return build_agent_instructions(
        MAIN_ADVISOR,
        active_skill_name=active_skill_name,
        skill_candidates=skill_candidates,
        artifact_context=artifact_context,
        trusted_action_context=trusted_action_context,
        background_jobs=background_jobs,
        client_file=client_file,
    )


def build_agent_instructions(
    definition: AgentDefinition,
    *,
    active_skill_name: Optional[str] = None,
    skill_candidates: Optional[Dict[str, list[Dict[str, Any]]]] = None,
    artifact_context: Optional[Dict[str, Any]] = None,
    trusted_action_context: Optional[Dict[str, Any]] = None,
    background_jobs: Optional[list[Dict[str, Any]]] = None,
    client_file: Optional[Dict[str, Any]] = None,
) -> str:
    """Combine base instructions, installed skill summaries, and active skill details."""

    installed_skills = DEFAULT_SKILL_REGISTRY.for_agent(definition.key)
    active_skill = _find_active_skill(installed_skills, active_skill_name)
    parts = [_static_instruction_prefix(definition, installed_skills)]
    if active_skill is not None:
        parts.append("# Active skill instructions\n\n" + format_skill_instructions(active_skill))
    else:
        activation_context = _format_skill_activation_context(
            definition.key,
            skill_candidates or {},
        )
        if activation_context:
            parts.append("# Skill activation context\n\n" + activation_context)
    resolved_jobs = _format_background_jobs(background_jobs)
    if resolved_jobs:
        parts.append("# Specialist jobs\n\n" + resolved_jobs)
    resolved_artifacts = _format_artifact_context(artifact_context)
    if resolved_artifacts:
        parts.append("# Resolved artifact and continuation context\n\n" + resolved_artifacts)
    resolved_action = _format_trusted_action_context(trusted_action_context)
    if resolved_action:
        parts.append("# Trusted authenticated action context\n\n" + resolved_action)
    if client_file is not None:
        pending_confirmation = _format_pending_fact_confirmation(client_file)
        if pending_confirmation:
            parts.append("# Pending fact confirmation context\n\n" + pending_confirmation)
        summary = _format_client_file_summary(client_file)
        parts.append(
            "# Current Client File\n\n"
            "Business summary of your durable state. Use tools for complete data.\n"
            + summary
        )
    return "\n\n".join(parts)


def _static_instruction_prefix(
    definition: AgentDefinition,
    installed_skills: Iterable[SkillDefinition],
) -> str:
    """Return cache-stable instruction parts 1–3."""

    parts = [
        "# Agent system instructions\n\n" + definition.system_instructions,
        "# Agent contract\n\n" + definition.instructions,
    ]
    summaries = _format_skill_summaries(installed_skills)
    if summaries:
        parts.append("# Installed skill summaries\n\n" + summaries)
    return "\n\n".join(parts)


def _format_background_jobs(
    background_jobs: Optional[list[Dict[str, Any]]],
) -> str:
    jobs = [
        job
        for job in background_jobs or []
        if isinstance(job, dict)
        and str(job.get("job_id") or "").strip()
    ][:12]
    if not jobs:
        return ""
    return (
        "This is trusted durable specialist-job state. You remain the only "
        "client-facing voice. For running work, say it is being prepared and do "
        "not promise success. Present a done result on this turn, including any "
        "staleness warning. Explain failed or cancelled work plainly and decide "
        "whether a fresh job is appropriate. Never present a running job as done.\n"
        + json.dumps(
            jobs,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    )


def _format_artifact_context(artifact_context: Optional[Dict[str, Any]]) -> str:
    if (
        not isinstance(artifact_context, dict)
        or artifact_context.get("schema_version")
        != "awm.artifact_reference_context.v1"
    ):
        return ""
    references = [
        {
            key: item.get(key)
            for key in ("domain", "analysis_id", "source_tool")
            if item.get(key) is not None
        }
        for item in artifact_context.get("references") or []
        if isinstance(item, dict)
        and str(item.get("analysis_id") or "").strip()
        and str(item.get("domain") or "").strip()
    ][:8]
    pending = (
        artifact_context.get("pending_operation")
        if isinstance(artifact_context.get("pending_operation"), dict)
        else None
    )
    if not references and pending is None:
        return ""
    compact = {
        "schema_version": "awm.artifact_reference_context.v1",
        "references": references,
        "pending_operation": pending,
    }
    return (
        "This is trusted server-resolved reference state, not client-authored financial "
        "facts. Use the exact immutable IDs when the user's reference is clear. The last "
        "reference in a domain is the most recent, but never replace two explicitly named "
        "analyses with a generic latest result. A pending operation may be resumed only "
        "when the new message semantically answers its bounded clarification.\n"
        + json.dumps(compact, ensure_ascii=False, separators=(",", ":"), default=str)
    )


def _format_trusted_action_context(
    trusted_action_context: Optional[Dict[str, Any]],
) -> str:
    if (
        not isinstance(trusted_action_context, dict)
        or trusted_action_context.get("schema_version")
        != "awm.trusted_action_context.v1"
    ):
        return ""
    return (
        "This is authenticated control-plane state, not client-authored speech "
        "or a Client File fact. The deterministic action already ran. Never "
        "repeat it, infer replacement IDs, or claim success when its status is "
        "failed. Follow the listed next-step constraints.\n"
        + json.dumps(
            trusted_action_context,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    )


def _find_active_skill(
    installed_skills: Iterable[SkillDefinition],
    active_skill_name: Optional[str],
) -> Optional[SkillDefinition]:
    if not active_skill_name:
        return None
    for skill in installed_skills:
        if skill.name == active_skill_name:
            return skill
    return None


def _format_skill_summaries(skills: Iterable[SkillDefinition]) -> str:
    lines = []
    for skill in skills:
        lines.append(
            f"- {skill.name}: {skill.summary} When to use: {skill.when_to_use}"
        )
    return "\n".join(lines)


def _format_skill_activation_context(
    agent_key: str,
    skill_candidates: Dict[str, list[Dict[str, Any]]],
) -> str:
    candidates = skill_candidates.get(agent_key) or []
    lines = [
        "No workflow skill is currently active for this agent.",
        "Use `activate_skill` before running business workflow tools when the user's intent and the current business state call for a workflow.",
        "For ordinary conversation, answer directly without activating a skill.",
    ]
    if not candidates:
        return "\n".join(lines)
    lines.append("Current workflow candidates from durable AWM state and the current turn:")
    for candidate in candidates[:6]:
        skill_name = str(candidate.get("skill_name") or "").strip()
        reason = str(candidate.get("reason") or "").strip()
        source = str(candidate.get("source") or "").strip()
        if not skill_name:
            continue
        detail = f"- {skill_name}"
        if source:
            detail += f" [{source}]"
        if reason:
            detail += f": {reason}"
        lines.append(detail)
    sources = {str(candidate.get("source") or "") for candidate in candidates}
    if "current_turn_material_facts" in sources:
        lines.extend(
            [
                "The current user message provides multiple material financial facts.",
                "Activate the recommended fact-writing skill and call `draft_fact` for each distinct fact in this same turn before acknowledging them as recorded.",
                "Do not only restate those facts conversationally.",
            ]
        )
    if "current_turn_proposal_construction" in sources or "durable_proposal_construction_ready" in sources:
        lines.extend(
            [
                "CRITICAL — a signed investment assessment exists and a proposal must be built. "
                "Call `consult_investment_solution_specialist` IMMEDIATELY. Do not present "
                "fact confirmations. Do not ask questions. Do not draft anything. Your only "
                "task this turn is to call the specialist. The specialist will produce the "
                "allocation, expected return, risk, and narrative. Narrate its result — do "
                "not invent numbers. If it fails, explain the failure plainly.",
            ]
        )
    if "current_turn_goal_fact" in sources:
        lines.extend(
            [
                "The current user message states a discrete goal with amount and horizon.",
                "Activate the recommended fact-writing skill and call `draft_fact` for that goal in this same turn.",
                "Do not treat a goal amount as an automatic investable amount without Financial Planning when sizing is needed.",
            ]
        )
    if "pending_draft_semantic_decision" in sources:
        lines.extend(
            [
                "Pending draft Client File facts require a semantic decision.",
                "Activate `confirm-facts`; commit only confirmed/corrected values and audit every decision.",
            ]
        )
    if "current_turn_investment_assessment_signoff" in sources:
        lines.extend(
            [
                "CRITICAL — the user wants to see the assessment NOW. Activate "
                "`investment-consult`, then call `consult_financial_planning_specialist` "
                "IMMEDIATELY and ask for an `internal investment assessment`. Do not "
                "verify facts. Do not ask questions. Create the assessment this turn.",
            ]
        )
    if "durable_proposal_ready" in sources:
        lines.extend(
            [
                "A signed assessment AND a completed proposal already exist on file.",
                "The client does NOT need a new assessment or proposal.",
                "Surface the existing proposal immediately — summarize the allocation, "
                "expected return, and risk. Offer to explain details, compare alternatives, "
                "or discuss next steps. Never say the client still needs an assessment.",
            ]
        )
    if "durable_assessment_creation_ready" in sources:
        lines.extend(
            [
                "CRITICAL — a money pool is fully defined but no investment assessment exists. "
                "Activate `investment-consult`, then call `consult_financial_planning_specialist` "
                "IMMEDIATELY in this same turn. Ask for an `internal investment assessment` for "
                "that pool. Do not ask more questions. Do not draft facts. Do not wait for the "
                "user to confirm. The pool is ready — create the assessment NOW.",
            ]
        )
    return "\n".join(lines)


def _format_client_file_summary(client_file: Dict[str, Any]) -> str:
    """Extract only decision-relevant fields — keep it under 5KB."""
    summary: Dict[str, Any] = {}

    # Facts — just values, not writeback metadata
    facts = client_file.get("facts")
    if isinstance(facts, dict) and facts:
        compact_facts = {}
        for key, val in facts.items():
            if isinstance(val, dict):
                compact_facts[key] = val.get("value") if "value" in val else val.get("as_stated", str(val)[:80])
            elif not isinstance(val, (list, dict)):
                compact_facts[key] = val
        if compact_facts:
            summary["facts"] = compact_facts

    # Money pools — just id, label, amount, purpose, horizon, risk
    pools = client_file.get("money_pools") or client_file.get("investment_pools") or []
    if isinstance(pools, list) and pools:
        summary["money_pools"] = [
            {k: p.get(k) for k in ("id", "pool_label", "amount", "purpose", "horizon_years", "risk") if p.get(k) is not None}
            for p in pools if isinstance(p, dict)
        ][:5]

    # Assessments — just id, version, status, verdict
    assessments = client_file.get("investment_assessments") or (
        (client_file.get("artifacts") or {}).get("plans") or []
    )
    if isinstance(assessments, list):
        filtered = []
        for a in assessments:
            if not isinstance(a, dict):
                continue
            p = a.get("payload", a)
            if isinstance(p, dict):
                filtered.append({
                    "assessment_id": p.get("assessment_id", a.get("assessment_id")),
                    "assessment_version": p.get("assessment_version", a.get("assessment_version")),
                    "status": p.get("status") or p.get("assessment_status"),
                    "verdict": (p.get("assessment") or {}).get("verdict") if isinstance(p.get("assessment"), dict) else None,
                    "money_pool_id": p.get("money_pool_id"),
                })
        if filtered:
            summary["assessments"] = filtered[:5]

    # Policies/proposals — just id, status
    policies = client_file.get("policies") or []
    if isinstance(policies, list) and policies:
        summary["policies"] = [
            {k: p.get(k) for k in ("id", "status", "policy_type", "money_pool_id") if p.get(k) is not None}
            for p in policies if isinstance(p, dict)
        ][:5]

    # Active consultation checkpoint
    checkpoint = client_file.get("active_consultation_checkpoint")
    if isinstance(checkpoint, dict):
        summary["active_checkpoint"] = {
            k: checkpoint.get(k)
            for k in ("skill", "phase", "next_question", "status")
            if checkpoint.get(k) is not None
        }

    # Advisor agenda — just top priorities
    agenda = client_file.get("advisor_agenda") or []
    if isinstance(agenda, list):
        summary["agenda"] = [
            {k: a.get(k) for k in ("subject", "phase", "priority") if a.get(k) is not None}
            for a in agenda if isinstance(a, dict) and a.get("priority", 0) >= 30
        ][:3]

    result = json.dumps(summary, ensure_ascii=False, default=str)
    if len(result) > 5000:
        result = result[:5000] + "\n... [summary truncated]"
    return result


def _format_pending_fact_confirmation(client_file: Dict[str, Any]) -> str:
    draft_facts = client_file.get("draft_facts") if isinstance(client_file, dict) else None
    if not isinstance(draft_facts, list) or not draft_facts:
        return ""
    preview = []
    for item in draft_facts[:6]:
        if not isinstance(item, dict):
            continue
        preview.append(
            {
                "draft_id": item.get("draft_id") or item.get("source_event_id"),
                "fact_type": item.get("fact_type"),
                "facts": item.get("facts"),
                "source_session_id": item.get("source_session_id"),
            }
        )
    return (
        "There are draft Client File facts waiting for client confirmation. "
        "Use the recent conversation to decide semantically whether the current user message confirms, "
        "corrects, declines, or ignores those drafts. To ask for confirmation, activate `confirm-facts` "
        "and call `present_fact_confirmation` with the exact draft IDs and fields shown; never ask only "
        "in prose. For a reply to a prompt-bound set, call `resolve_fact_confirmation` for only its "
        "presented item IDs. Use `commit_facts` only for an explicit confirmation of legacy unbound "
        "drafts. A short yes/confirm after a fact readback is enough to resolve those items.\n\n"
        "Pending drafts:\n"
        + json.dumps(preview, ensure_ascii=False, default=str)
    )


def format_skill_instructions(skill: SkillDefinition) -> str:
    return (
        f"{skill.name}: {skill.summary}\n"
        f"When to use: {skill.when_to_use}\n\n"
        f"{skill.instructions}"
    )

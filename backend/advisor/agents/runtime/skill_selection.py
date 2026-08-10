from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from client_file.lifecycle import has_pending_draft_facts

from advisor.agents.context import AwmAgentContext
from advisor.agents.runtime.client_file_state import (
    _client_file_has_proposed_policy,
    _client_file_has_signed_assessment,
    _client_file_ready_for_assessment_creation,
    _client_file_ready_for_proposal_construction,
)
from advisor.agents.skills import DEFAULT_SKILL_REGISTRY


def _set_initial_active_skill(
    context: AwmAgentContext,
    *,
    agent_key: str,
    explicit_active_skill: Optional[str],
    recent_history: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Activate only installed skills and otherwise expose advisory candidates."""

    if explicit_active_skill:
        installed_skill_names = {
            skill.name
            for skill in DEFAULT_SKILL_REGISTRY.for_agent(agent_key)
        }
        if explicit_active_skill in installed_skill_names:
            context.active_skills[agent_key] = explicit_active_skill
            return

    candidates = _build_skill_candidates(
        context.client_file,
        agent_key=agent_key,
        user_message=context.user_message,
        active_skills=context.active_skills,
        recent_history=recent_history,
    )
    if candidates:
        context.skill_candidates[agent_key] = candidates


def _activation_only_continuation_tools(
    context: AwmAgentContext,
    *,
    agent_key: str,
) -> Set[str]:
    """Require one bounded action after semantic skill activation.

    Skill selection remains model-owned. This helper only prevents a successful
    ``activate_skill`` call from becoming a terminal no-op. The continuation is
    limited to the selected skill's registered tools plus the structured
    clarification escape hatch.
    """

    if context.mid_turn_commit_refresh_verified is False:
        return set()
    tool_results = [
        result
        for result in context.tool_results
        if isinstance(result, dict) and str(result.get("tool") or "").strip()
    ]
    latest_tool = str((tool_results[-1] if tool_results else {}).get("tool") or "").strip()
    if latest_tool != "activate_skill":
        # A completed business-tool attempt is not an activation-only no-op,
        # even when its result is blocked after deterministic validation.
        return set()

    successful_results = [
        result
        for result in tool_results
        if result.get("ok") is not False
    ]
    latest_successful_tool = next(
        (
            str(result.get("tool") or "").strip()
            for result in reversed(successful_results)
            if str(result.get("tool") or "").strip()
        ),
        "",
    )
    if latest_successful_tool != "activate_skill":
        return set()

    active_skill = str(context.active_skills.get(agent_key) or "").strip()
    if not active_skill:
        active_skill = next(
            (
                str(result.get("active_skill") or "").strip()
                for result in reversed(successful_results)
                if str(result.get("tool") or "").strip() == "activate_skill"
                and str(result.get("active_skill") or "").strip()
            ),
            "",
        )
        if active_skill:
            context.active_skills[agent_key] = active_skill
    if not active_skill:
        return set()
    try:
        skill = DEFAULT_SKILL_REGISTRY.get(active_skill)
    except KeyError:
        return set()
    if agent_key not in skill.allowed_agents:
        return set()

    already_successful_tools = {
        str(result.get("tool") or "").strip()
        for result in successful_results
        if str(result.get("tool") or "").strip() != "activate_skill"
    }
    if "commit_facts" in already_successful_tools:
        already_successful_tools.update({"draft_fact", "save_fact"})
    return (
        set(skill.tool_names) - already_successful_tools
    ) | {"request_clarification"}


def _post_fact_continuation_tools(
    context: AwmAgentContext,
    *,
    agent_key: str,
) -> Set[str]:
    """Expose one bounded continuation for an agent-recorded pending calculation.

    The model authors the action in ``commit_facts``. This helper does not infer
    intent from the user's words; it only verifies that the commit succeeded,
    its refreshed state is usable, and the calculation has not already run.
    """

    if agent_key != "main_advisor":
        return set()
    if context.post_fact_continuation_attempted:
        return set()
    if context.post_fact_continuation_action != "cashflow_projection":
        return set()
    if context.mid_turn_commit_refresh_verified is False:
        return set()
    successful_commit = any(
        isinstance(result, dict)
        and result.get("tool") == "commit_facts"
        and result.get("ok") is True
        and result.get("post_commit_action") == "cashflow_projection"
        for result in context.tool_results
    )
    if not successful_commit:
        return set()
    if any(
        isinstance(result, dict)
        and result.get("tool") == "run_cashflow_projection"
        for result in context.tool_results
    ):
        return set()
    return {
        "consult_financial_planning_specialist",
        "request_clarification",
    }


def _current_turn_requests_investment_assessment_signoff(user_message: str) -> bool:
    normalized = " ".join(str(user_message or "").lower().replace("_", " ").replace("-", " ").split())
    if not normalized:
        return False
    return (
        "investment assessment" in normalized
        or "investment consultation assessment" in normalized
        or "consultation assessment" in normalized
        or "assessment summary" in normalized
        or "recommendation summary" in normalized
        or ("prepare" in normalized and "assessment" in normalized)
        or ("show me" in normalized and "assessment" in normalized)
        or "ready for sign off" in normalized
        or "ready for signoff" in normalized
        or ("whether to agree" in normalized and "recommend" in normalized)
    )


_PROJECTION_ASK_TERMS = (
    "on track",
    "look ahead",
    "long term",
    "longer term",
    "future",
    "retire",
    "retirement",
    "sustainable",
    "how things might look",
    "how things look",
    "financially okay",
    "be okay",
    "enough",
    "projection",
    "forecast",
    "outlook",
    "down the road",
    "next 10",
    "next 15",
    "next 20",
    "next 5",
    "cash flow",
    "cashflow",
    "net-worth",
    "net worth",
)

_PROJECTION_SOLICIT_TERMS = (
    "growth rate",
    "home's value",
    "home value",
    "home appreciation",
    "annual growth",
    "assumption",
    "assumptions",
    "mortgage term",
    "years left on the mortgage",
    "spending already includes",
    "before i show",
    "before i run",
    "before i refresh",
    "cash-flow",
    "cash flow",
    "cashflow",
    "projection",
    "forecast",
    "outlook",
)

_PLANNING_CONTINUITY_SKILLS = frozenset(
    {"regular-consult", "investment-consult", "policy-review"}
)


def _client_file_has_saved_facts(client_file: Dict[str, Any]) -> bool:
    if not isinstance(client_file, dict):
        return False
    facts = client_file.get("facts")
    if isinstance(facts, dict) and len(facts) > 0:
        return True
    structured = client_file.get("structured_facts")
    return isinstance(structured, dict) and len(structured) > 0


def _message_looks_like_projection_ask(user_message: str) -> bool:
    msg = " ".join(str(user_message or "").lower().split())
    if not msg:
        return False
    return any(term in msg for term in _PROJECTION_ASK_TERMS)


def _message_looks_like_short_assumption_answer(user_message: str) -> bool:
    """Catch short replies to a prior projection-assumption question (e.g. '3%', 'okk')."""

    text = " ".join(str(user_message or "").strip().lower().split())
    if not text or len(text) > 48:
        return False
    if text.rstrip(".!?") in {
        "yes",
        "yep",
        "yeah",
        "ok",
        "okay",
        "okk",
        "sure",
        "correct",
        "confirmed",
        "go ahead",
        "finish",
        "done",
        "please",
    }:
        return True
    if re.fullmatch(r"\d+(\.\d+)?%?", text.rstrip(".!?")):
        return True
    if re.fullmatch(
        r"(about |around |use |assume )?\d+(\.\d+)?%?( a year| per year| annually)?",
        text.rstrip(".!?"),
    ):
        return True
    if re.fullmatch(r"(about |around )?\d+(\.\d+)? years?", text.rstrip(".!?")):
        return True
    return False


def _recent_assistant_solicited_projection_input(
    recent_history: Optional[List[Dict[str, Any]]],
) -> bool:
    if not recent_history:
        return False
    for item in reversed(list(recent_history)[-8:]):
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "").lower() != "assistant":
            continue
        content = " ".join(str(item.get("content") or item.get("text") or "").lower().split())
        if content and any(term in content for term in _PROJECTION_SOLICIT_TERMS):
            return True
    return False


def _build_skill_candidates(
    client_file: Dict[str, Any],
    *,
    agent_key: str,
    user_message: str = "",
    active_skills: Optional[Dict[str, str]] = None,
    recent_history: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Summarize durable workflow state for the agent without activating a skill."""

    candidates: List[Dict[str, Any]] = []
    _has_saved_facts = _client_file_has_saved_facts(
        client_file if isinstance(client_file, dict) else {}
    )
    _prev_skill = str((active_skills or {}).get(agent_key, "") or "").strip()
    _is_projection_ask = (
        agent_key == "main_advisor" and _message_looks_like_projection_ask(user_message)
    )
    _is_projection_followup = bool(
        agent_key == "main_advisor"
        and _has_saved_facts
        and _message_looks_like_short_assumption_answer(user_message)
        and (
            _prev_skill in _PLANNING_CONTINUITY_SKILLS
            or _recent_assistant_solicited_projection_input(recent_history)
        )
    )
    _prefer_projection = _is_projection_ask or _is_projection_followup
    # Once facts are on file, do not re-inject onboarding while the user is in a
    # planning thread (explicit ask, short follow-up to an assumption question, or
    # a non-onboarding active skill). Onboarding rules block mid-turn saves and FP tools.
    _suppress_onboarding = bool(
        agent_key == "main_advisor"
        and _has_saved_facts
        and (
            _prefer_projection
            or (_prev_skill and _prev_skill != "onboarding-consult")
        )
    )

    def _append_candidate(skill_name: str, *, source: str, reason: str) -> None:
        name = str(skill_name or "").strip()
        if not name:
            return
        if name == "onboarding-consult" and _suppress_onboarding:
            return
        candidates.append(
            {
                "skill_name": name,
                "source": source,
                "reason": reason,
            }
        )

    workflow_skill = _infer_actionable_workflow_skill(client_file, agent_key=agent_key)
    if workflow_skill:
        _append_candidate(
            workflow_skill,
            source="durable_workflow_state",
            reason="Client File contains an unfinished investment workflow or recent investment writeback.",
        )

    if agent_key == "main_advisor" and has_pending_draft_facts(
        client_file if isinstance(client_file, dict) else {}
    ):
        _append_candidate(
            "confirm-facts",
            source="pending_draft_semantic_decision",
            reason="Pending draft facts require a semantic confirmation decision.",
        )

    if agent_key == "main_advisor" and _current_turn_requests_investment_assessment_signoff(user_message):
        _append_candidate(
            "investment-consult",
            source="current_turn_investment_assessment_signoff",
            reason=(
                "The current user message asks AWM to prepare or summarize an investment consultation "
                "assessment for client sign-off; activate investment-consult and create the structured "
                "assessment card before presenting it as ready for sign-off."
            ),
        )

    if agent_key == "main_advisor" and _client_file_ready_for_assessment_creation(
        client_file if isinstance(client_file, dict) else {}
    ):
        _append_candidate(
            "investment-consult",
            source="durable_assessment_creation_ready",
            reason=(
                "A money pool is defined with amount, purpose, horizon, and risk, and no investment "
                "assessment exists yet; activate investment-consult and call "
                "consult_financial_planning_specialist to create the pending assessment before "
                "asking more optional preference questions."
            ),
        )

    if agent_key == "main_advisor" and _client_file_ready_for_proposal_construction(
        client_file if isinstance(client_file, dict) else {}
    ):
        _append_candidate(
            "investment-consult",
            source="durable_proposal_construction_ready",
            reason=(
                "A signed investment assessment is on file and no proposed policy exists yet; "
                "activate investment-consult and call consult_investment_solution_specialist "
                "so run_asset_allocation can produce the proposal artifact."
            ),
        )

    if (
        agent_key == "main_advisor"
        and _client_file_has_signed_assessment(
            client_file if isinstance(client_file, dict) else {}
        )
        and _client_file_has_proposed_policy(
            client_file if isinstance(client_file, dict) else {}
        )
    ):
        _append_candidate(
            "investment-consult",
            source="durable_proposal_ready",
            reason=(
                "A signed assessment AND a ready proposal already exist on file. "
                "The proposal is complete — do NOT say the client still needs an assessment or "
                "proposal. Surface the existing proposal, offer to explain it, and ask if "
                "the client wants to review, refine, or take action."
            ),
        )

    if isinstance(client_file, dict):
        checkpoint = client_file.get("active_consultation_checkpoint")
        if isinstance(checkpoint, dict) and str(checkpoint.get("status") or "").lower() not in {
            "complete",
            "completed",
            "resolved",
        }:
            consultation_type = str(checkpoint.get("consultation_type") or "").strip()
            if consultation_type:
                _append_candidate(
                    consultation_type,
                    source="active_consultation_checkpoint",
                    reason="Client File has an unfinished consultation checkpoint.",
                )

    # Projection intent / short follow-up to an assumption question. Runs before
    # state fallback so regular-consult can win over onboarding candidates.
    if _prefer_projection:
        _append_candidate(
            "regular-consult",
            source=(
                "current_turn_projection_followup"
                if _is_projection_followup and not _is_projection_ask
                else "current_turn_projection_intent"
            ),
            reason=(
                "The user is answering a projection assumption or asking about their long-term "
                "financial future. Activate regular-consult and call "
                "consult_financial_planning_specialist to save any answered assumptions and run "
                "or refresh the cashflow projection. Ask only for remaining required inputs — "
                "do not restart full onboarding."
            ),
        )

    fallback_skill = _infer_state_active_skill(client_file, agent_key=agent_key)
    if fallback_skill:
        _append_candidate(
            fallback_skill,
            source="state_fallback",
            reason="Client File state suggests this workflow may be relevant.",
        )

    # ── post-confirmation resumption ──
    # When facts were just confirmed and there's an active workflow checkpoint,
    # inject a strong signal to resume the prior objective immediately.
    if (
        agent_key == "main_advisor"
        and isinstance(client_file, dict)
        and not has_pending_draft_facts(client_file)
    ):
        checkpoint = client_file.get("active_consultation_checkpoint")
        if isinstance(checkpoint, dict) and str(checkpoint.get("status") or "").lower() not in {
            "complete", "completed", "resolved",
        }:
            _cp_skill = str(checkpoint.get("skill") or "").strip()
            if _cp_skill:
                _append_candidate(
                    _cp_skill,
                    source="current_turn_post_confirmation_resume",
                    reason=(
                        f"Facts were just confirmed and the {_cp_skill} workflow has an "
                        "active checkpoint. Resume this workflow immediately — do not wait "
                        "for the user to prompt you. Continue from the checkpoint's next "
                        "question or objective."
                    ),
                )

    # ── active objective continuation ──
    # When the previous turn had a workflow skill active (investment-consult etc.)
    # and this turn has competing lower-priority signals, inject a high-priority
    # continuation candidate so the agent doesn't lose the thread.
    if active_skills and agent_key in active_skills:
        prev_skill = str(active_skills[agent_key]).strip()
        if prev_skill in _PLANNING_CONTINUITY_SKILLS:
            _competing = [
                c for c in candidates if str(c.get("skill_name") or "").strip() != prev_skill
            ]
            if _competing:
                _append_candidate(
                    prev_skill,
                    source="active_objective_continuation",
                    reason=(
                        f"The previous turn was actively engaged in {prev_skill}. "
                        "Continue this workflow — do not switch to a different skill "
                        "unless the user explicitly changes the subject. Fact confirmations "
                        "and onboarding completions can wait until this objective is resolved."
                    ),
                )

    deduped: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    source_rank = {
        "current_turn_": 100,
        "active_objective_continuation": 95,
        "durable_proposal_construction_ready": 80,
        "durable_assessment_creation_ready": 70,
        "durable_workflow_state": 60,
        "active_consultation_checkpoint": 40,
        "state_fallback": 20,
    }

    def _source_rank(source: str) -> int:
        if source.startswith("current_turn_"):
            return source_rank["current_turn_"]
        return source_rank.get(source, 0)

    for candidate in candidates:
        skill_name = str(candidate.get("skill_name") or "").strip()
        if not skill_name:
            continue
        source = str(candidate.get("source") or "").strip()
        if skill_name in seen:
            existing = deduped[seen[skill_name]]
            # Prefer higher-signal sources for the same skill (current-turn > proposal-ready > durable).
            if _source_rank(source) > _source_rank(str(existing.get("source") or "")):
                existing["source"] = source
                if candidate.get("reason"):
                    existing["reason"] = candidate.get("reason")
            continue
        seen[skill_name] = len(deduped)
        deduped.append(candidate)
    return deduped


def _history_records_confirmation_decision(items: List[Dict[str, Any]]) -> bool:
    def contains(value: Any) -> bool:
        if isinstance(value, dict):
            if value.get("tool") == "record_confirmation_decision" and value.get("ok") is not False:
                return True
            if value.get("confirmation_decision") in {"confirmed", "rejected", "corrected", "ambiguous"}:
                return True
            return any(contains(item) for item in value.values())
        if isinstance(value, list):
            return any(contains(item) for item in value)
        return False
    return any(contains(item) for item in items)


def _previous_user_message(history: List[Dict[str, Any]]) -> str:
    for index in range(len(history) - 1, -1, -1):
        item = history[index]
        if str(item.get("role") or "").lower() != "user":
            continue
        content = str(item.get("content") or "").strip()
        normalized = " ".join(content.lower().split()).rstrip(".!?")
        later_turn_items = []
        for later in history[index + 1:]:
            if str(later.get("role") or "").lower() == "user":
                break
            later_turn_items.append(later)
        if not content or _history_records_confirmation_decision(later_turn_items):
            continue
        if normalized in {
            "rerun analysis",
            "rerun the analysis",
            "run analysis",
            "run the analysis",
            "rerun projection",
            "rerun the projection",
            "refresh the projection",
            "update the projection",
            "run the projection",
            "run the projection again",
            "yes refresh the projection",
            "yes, refresh the projection",
            "yes please refresh",
            "yes, please refresh",
            "please refresh",
            "go ahead and refresh",
        }:
            continue
        return content
    return ""


def _expand_rerun_request(user_message: str, history: List[Dict[str, str]]) -> str:
    """Bind a short rerun command to the last substantive planning request."""

    normalized = " ".join(str(user_message or "").lower().strip().split()).rstrip(".!?")
    if normalized not in {
        "rerun analysis",
        "rerun the analysis",
        "run analysis",
        "run the analysis",
        "rerun projection",
        "rerun the projection",
        "refresh the projection",
        "update the projection",
        "run the projection",
        "run the projection again",
        "yes refresh the projection",
        "yes, refresh the projection",
        "yes please refresh",
        "yes, please refresh",
        "please refresh",
        "go ahead and refresh",
    }:
        return user_message
    for index in range(len(history) - 1, -1, -1):
        item = history[index]
        if str(item.get("role") or "").lower() != "user":
            continue
        content = str(item.get("content") or "").strip()
        lowered = content.lower()
        later_turn_items = []
        for later in history[index + 1:]:
            if str(later.get("role") or "").lower() == "user":
                break
            later_turn_items.append(later)
        if not content or _history_records_confirmation_decision(later_turn_items):
            continue
        if not re.search(
            r"\b(retire|retirement|projection|cashflow|cash flow|afford|how much|"
            r"investment capacity|shortfall|depletion|liquidity)\b",
            lowered,
        ):
            continue
        return (
            "Rerun the prior quantitative planning request using only the newly confirmed "
            "Client File values. Preserve the original scenario intent and do not invent a "
            "scenario change whose amount or value was not stated.\n\nOriginal client request:\n"
            + content
        )
    return user_message


def _infer_state_active_skill(client_file: Dict[str, Any], *, agent_key: str) -> Optional[str]:
    """Recover the active AWM skill from durable Client File workflow state."""

    workflow_skill = _infer_actionable_workflow_skill(client_file, agent_key=agent_key)
    if workflow_skill:
        return workflow_skill

    if agent_key != "main_advisor" or not isinstance(client_file, dict):
        return None

    if _onboarding_objective_is_complete(client_file):
        return None

    if _client_file_needs_first_time_onboarding(client_file):
        return "onboarding-consult"

    return None


def _infer_actionable_workflow_skill(client_file: Dict[str, Any], *, agent_key: str) -> Optional[str]:
    """Infer a business workflow skill from actionable persisted state, excluding fallbacks."""

    if agent_key != "main_advisor" or not isinstance(client_file, dict):
        return None

    recent_writebacks = client_file.get("recent_writebacks")
    if isinstance(recent_writebacks, list):
        for writeback in recent_writebacks[:8]:
            if not isinstance(writeback, dict):
                continue
            operation = str(writeback.get("operation") or "").strip()
            values = writeback.get("values") if isinstance(writeback.get("values"), dict) else {}
            if operation == "record_assessment_signoff" and values.get("signed_off") is True:
                return "investment-consult"
            if operation == "upsert_money_pool":
                return "investment-consult"

    open_loops = client_file.get("open_loops")
    if isinstance(open_loops, list):
        for loop in open_loops:
            if not isinstance(loop, dict):
                continue
            loop_type = str(loop.get("type") or "").strip()
            status = str(loop.get("status") or "").strip().lower()
            if loop_type == "onboarding_incomplete" and status not in {"complete", "completed", "resolved", "closed"}:
                return "onboarding-consult"
            if loop_type.startswith("money_pool") and status not in {"complete", "completed", "resolved", "closed"}:
                return "investment-consult"

    objectives = client_file.get("engagement_objectives")
    if isinstance(objectives, list):
        for objective in objectives:
            if not isinstance(objective, dict):
                continue
            name = str(objective.get("objective") or "").strip()
            status = str(objective.get("status") or "").strip().lower()
            if name.startswith("complete_investment") and status not in {"complete", "completed", "resolved", "closed"}:
                return "investment-consult"
            if name.startswith("onboarding") and status in {"complete", "completed", "resolved", "closed"}:
                return None

    return None


def _onboarding_objective_is_complete(client_file: Dict[str, Any]) -> bool:
    objectives = client_file.get("engagement_objectives")
    if not isinstance(objectives, list):
        return False
    for objective in objectives:
        if not isinstance(objective, dict):
            continue
        name = str(objective.get("objective") or "").strip()
        status = str(objective.get("status") or "").strip().lower()
        if name.startswith("onboarding") and status in {"complete", "completed", "resolved", "closed"}:
            return True
    return False


def _client_file_needs_first_time_onboarding(client_file: Dict[str, Any]) -> bool:
    """Use durable state, not keyword routing, to keep first-time discovery inside onboarding."""

    onboarding = client_file.get("onboarding")
    if isinstance(onboarding, dict):
        status = str(onboarding.get("advisor_onboarding_status") or "").lower()
        if status in {"complete", "completed", "done"}:
            return False
    completeness = client_file.get("onboarding_completeness")
    if isinstance(completeness, dict) and completeness.get("complete") is True:
        return False

    for key in ("facts", "structured_facts"):
        value = client_file.get(key)
        if isinstance(value, dict) and len(value) >= 6:
            return False
    draft_facts = client_file.get("draft_facts")
    if isinstance(draft_facts, list) and len(draft_facts) >= 6:
        return False
    return True

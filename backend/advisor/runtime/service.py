"""Production composition root for the AWM text advisor runtime."""

from __future__ import annotations

from advisor.agents.runtime import AwmAgentsRuntime


def build_production_advisor_runtime() -> AwmAgentsRuntime:
    """Bind the dependency-injected SDK runtime to production repositories."""

    from api.persistence import (
        get_asset_allocation_analysis_snapshot,
        get_cashflow_analysis_snapshot,
        iter_companion_messages_before,
        iter_companion_messages_since,
        list_active_summaries,
        retrieve_conversation_history,
        upsert_asset_allocation_analysis,
        upsert_cashflow_analysis,
        upsert_investment_assessment,
    )
    from advisor.tools.deterministic_tools.execution import (
        ContractOnlyFinancialPlanningQueryService,
        RegistryToolExecutor,
        V2PersistentToolExecutor,
        build_production_subagents,
    )
    from client_file.repository import build_production_client_file_repository
    from api.persistence.assumptions import get_assumption_repository
    from api.services.assumption_research import (
        build_runtime_public_fact_promotion_service,
        build_runtime_session_research_specialist,
    )
    from api.services.fact_confirmations import FactConfirmationRepository

    repository = build_production_client_file_repository()
    assumption_repository = get_assumption_repository()

    def _compact_conversation_if_needed(**kwargs):
        from api.server.deps import get_conversation_memory_service

        return get_conversation_memory_service().compact_if_needed(**kwargs)

    financial_planning, _investment_solution, _revalidation = (
        build_production_subagents()
    )
    executor = V2PersistentToolExecutor(
        fallback=RegistryToolExecutor(
            client_file_writer=repository.writer,
            client_file_reader=repository.reader,
            investment_assessment_store=upsert_investment_assessment,
            cashflow_analysis_store=upsert_cashflow_analysis,
            cashflow_analysis_reader=get_cashflow_analysis_snapshot,
            asset_allocation_analysis_store=upsert_asset_allocation_analysis,
            asset_allocation_analysis_reader=get_asset_allocation_analysis_snapshot,
            conversation_history_reader=retrieve_conversation_history,
            fact_confirmation_repository=FactConfirmationRepository(),
            public_fact_research_service=(
                build_runtime_session_research_specialist(
                    repository=assumption_repository,
                )
            ),
            public_fact_promotion_service=(
                build_runtime_public_fact_promotion_service(
                    repository=assumption_repository,
                )
            ),
            financial_planning_query_service=(
                ContractOnlyFinancialPlanningQueryService(
                    financial_planning_agent=financial_planning,
                )
            ),
        )
    )
    return AwmAgentsRuntime(
        client_file_reader=repository.reader,
        tool_executor=executor,
        conversation_history_reader=iter_companion_messages_since,
        conversation_history_reverse_reader=iter_companion_messages_before,
        conversation_summary_reader=list_active_summaries,
        conversation_compaction_runner=_compact_conversation_if_needed,
    )


__all__ = ["build_production_advisor_runtime"]

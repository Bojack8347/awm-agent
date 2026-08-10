---
name: cashflow-analysis
summary: Runs validated cash-flow analysis and returns typed evidence or one missing-input question.
when_to_use: Use for a quantitative household planning request delegated by the Main Advisor.
allowed_agents:
  - financial_planning
capabilities:
  - cashflow_projection
  - cashflow_retrieval
  - calculation_toolkit
  - external_math_lookup
  - public_fact_research
  - public_fact_reuse_review
---

# cashflow-analysis

Use this skill for a quantitative household planning request delegated by the Main Advisor.

1. For a new baseline or changed what-if request, call `run_cashflow_projection` before producing the
   final specialist response. Do this even when the Client File may be incomplete, because the
   deterministic result owns the exact missing-data list and next question.
   If the delegated request explicitly says to run, rerun, project, or simulate, call the read-only
   model in this turn; do not ask for separate consent to perform the analysis.
2. For a follow-up about an already completed result with no changed model input, call
   `get_cashflow_analysis` with the referenced `analysis_id`, or null for the latest result in the
   current conversation. Do not rerun Monte Carlo merely to explain metrics, percentiles, event timing,
   assumptions, limitations, or implications already present in the stored evidence.
   For arithmetic, verification, reconciliation, comparison, and goal-solving requests, use the
   matching registered calculation tool against the exact immutable `analysis_id`; never calculate
   in model prose.
   Decide whether arithmetic is actually needed. For formula-based arithmetic not owned by a
   purpose-built audit, comparison, or solver, submit one complete typed local plan when the
   registered operations can represent the request. If they cannot, use `query_wolfram_alpha` at
   most once and only for a de-identified pure-math problem; never send client facts, values,
   analysis ids, or governed financial logic. Otherwise call `report_calculation_capability_gap`.
   This three-way choice belongs to you; deterministic validation rejects unsafe or invalid requests
   but does not route the question.
   You own the only two web-research triggers. For a new projection, first call
   `run_cashflow_projection` from the current Client File and ask the smallest question for an exact
   missing model input. Research only after the user says they do not know it or when its required
   configured default is absent, and only if you judge that a supported public government fact can
   fill that exact input. Separately, for a conversational follow-up, research only when you judge that
   a supported public fact is necessary to answer it and trusted evidence does not already contain it.
   Do not use a keyword rule or research merely because a public fact is mentioned.
   Call `research_public_financial_fact` with only the canonical variable and exact effective year.
   Never submit a free-form query, arbitrary URL, Client File data, planning assumption, forecast, or
   recommendation. The server searches only the configured official authority website. For a missing
   projection input, retain only its model-ready value, dimensional unit, effective year, and citations.
   The validated session fact is immediately reportable and eligible for supported local arithmetic
   without human approval.
   After research, decide whether the exact fact is appropriate to store and reuse, then call
   `review_public_fact_reuse` with the returned `session_fact_id`, your decision, and its paired reason
   code. Server checks of session binding, source, year, units, freshness, and persistence are mechanical
   validation, not routing. Then decide whether authorized inputs justify a projection, recommendation,
   calculation, or explanation. Neither research nor reuse authorization forces a downstream action.
   Never copy the researched scalar into a literal or unsupported cash-flow field. For eligible local
   arithmetic, a `session_public_fact` source contains exactly `id`, `kind`, and `session_fact_id`;
   never add `selector`, `unit`, or `value`. Pass only a server-issued opaque projection
   reference when the review result provides one. Otherwise preserve the capability gap.
   For an exact stored calendar year, pass `calendar_years` and the required `detail_columns`;
   never interpolate. If retrieval says a report column was not collected, rerun only when the
   client actually needs it and select the smallest matching `detail_report_groups`.
   When a new projection request already names exact years, pass `calendar_years` and canonical
   `detail_columns` directly to `run_cashflow_projection` along with the matching report groups.
   The run returns a bounded exact-year excerpt from its newly stored annual series; a second
   retrieval call is unnecessary.
   When the same delegated request needs arithmetic over a newly completed projection, continue from
   its returned immutable `analysis_id` to the calculation plan in this invocation. Do not rerun or
   retrieve that projection first.
3. Determine which supported baseline variables must change from the user's intent, then submit the
   corresponding structured `scenario_changes` using only `kind` values and fields allowed by the
   tool schema. Do not restate existing Client File data, perform hand calculations, or invent
   missing values in the tool request.
   Also write `scenario_summary` and `scenario_rationale` as concise declarative records of what the
   run tests and why the selected changes match the user's intent. For a current-plan baseline, say
   that no variables change and why the baseline is needed. Do not provide hidden chain-of-thought,
   calculations, unsupported assumptions, or advice in these fields.
   If the exact requested change is not representable by that public schema, do not substitute a
   nearby change or run an approximation. Call `report_calculation_capability_gap` with
   `unsupported_operation`; this is your semantic decision, not a keyword or server routing rule.
   If the Main Advisor supplies one exact completed allocation `analysis_id`, pass it unchanged as
   `allocation_analysis_id`. If it supplies several analyses for distinct confirmed money pools,
   pass all exact ids as `allocation_analysis_ids`. Do not transcribe weights, choose a different
   allocation, or infer a money-pool/account mapping.
   Keep `detail_report_groups` null for the fast Net Worth/Shortfall Debt/Bank Balance path. Select
   only the explicit `income`, `spending`, `taxes`, `withdrawals`, `account_balances`, or `mortgage`
   groups needed by a question about annual decomposition.
   Pass `monte_carlo_paths=null` for a deterministic projection. For probabilistic analysis, select
   an allowed path count and honor the client's count when specified. Explain that more paths reduce
   sampling noise but do not improve assumptions, remove model risk, or create a guarantee.
   Supported bounded events include future or temporary income changes, a household-spending-growth
   override, spouse retirement age, and recurring taxable-investment contributions. Supply explicit
   timing, duration, person, and amount/rate fields; never infer them.
   A current retirement-account contribution is a Client File baseline fact, not a
   `recurring_investment_contribution` scenario change. That scenario change is taxable-brokerage-only;
   never use it for a 401(k), IRA, or generic retirement-savings contribution.
   For an exact monthly contribution question, use the bounded contribution solver with the available
   analysis evidence and preserve its reported bounds and limitations.
4. If retrieval reports `cashflow_analysis_stale`, do not use its old numbers; explain that relevant
   Client File inputs changed and rerun only when the user's question calls for an updated projection.
5. If the result is not reportable, return its smallest `next_question` or missing-input explanation and
   do not provide a numerical conclusion.
6. If the result is reportable, preserve its typed evidence references, permission level, warnings,
   and deterministic `cashflow_agent_view.interpretations`. Never turn a reporting-only interpretation
   into a recommendation.
7. Present cash-flow results as a planning explanation rather than a raw metric dump:
   - Default to enough depth that the client can understand the result without another request for
     interpretation. Except for a pure clarification, do not stop at a bare number or one-line tool
     result. Do not add filler, repeat every metric, or exceed the evidence's `permitted_use`.
   - Keep the richer explanation evidence-safe. Every numeric statement must be an exact typed claim
     returned by a tool in the current turn or a fact supplied in the current user message. Do not
     carry a path count or another number forward from conversation prose, derive a probability
     complement, or introduce a new numeric interpretation. Explain practical meaning qualitatively
     when the current tool does not expose the supporting number as a typed claim.
   - State the run type and path count first. A deterministic baseline is one path; describe its
     success value as pass/fail, never as a Monte Carlo probability, and do not present identical
     p10/p50/p90 bands as separate possible outcomes.
   - Lead with the two or three decision-relevant outcomes, then explain what drove them, which
     confirmed inputs/defaults matter most, and the smallest next analysis step.
   - For a scenario comparison, identify the baseline and changed inputs, report the supported
     difference, and explain its practical meaning. Describe a modeled association, not causation,
     unless separately authorized evidence establishes causation.
   - For an arithmetic follow-up, give the direct result, name the compared operands, and explain
     what the result does and does not mean for the plan. When both compared values are negative,
     explicitly say that an improvement is a smaller modeled deficit, not a successful plan. Include
     one material limitation and, when supported, one useful next comparison. Never create extra
     arithmetic in prose. Mention prior projection metrics or path counts only when the current
     calculation result returns them as typed claims.
     Write for a client with no financial background and use this four-part structure: (1) explain
     the compared operands in plain language, (2) state the direct result, (3) explain what it means
     and what it does not mean for the plan, and (4) ask whether the client wants one optional,
     supported follow-up calculation. Phrase the last part neutrally as "If you want, I can...";
     do not use "recommend" or "should" and do not suggest a financial action. A formula or number
     without the plain-language explanation is incomplete.
     Before responding, validate your explanation against the returned calculation trace: every
     number must match a typed output or resolved source, every financial label must match its source
     metric and value path, the comparison direction must match the executed operation, and your
     interpretation must preserve the operands' signs. Correct any mismatch before giving the direct
     result and its practical meaning.
     For a requested percentage, use `percentage_change` when applicable; if a supported formula
     requires `divide`, apply `as_percentage` before returning that output. Never present a raw decimal
     ratio as the requested percentage.
   - For a capability gap, name the exact unsupported dimension, explain why the requested result
     cannot be produced faithfully, and offer at most one clearly labeled supported alternative.
     Do not imply that the alternative is equivalent. For missing or ambiguous inputs, remain brief:
     ask only the single smallest question needed to continue.
   - Distinguish terminal cumulative cash-flow shortfall debt from a present-day savings gap. Never
     tell the client that the terminal debt value is the amount to deposit today.
   - When `analysis_persistence.stored=true`, tell the client the validated result is saved for
     unchanged follow-up questions. Use `get_cashflow_analysis` later instead of rerunning; rerun only
     for changed inputs, a requested higher path count, or an uncollected report series.
   - When an audit finds a contradiction, preserve the source analysis and explain that its
     reporting evidence requires a corrected rerun. Never silently substitute audit arithmetic
     for the official model result.

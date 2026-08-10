---
name: assessment-revalidation
summary: Revalidates a signed investment assessment after material Client File changes.
when_to_use: Use when a prior signed assessment may have become stale after a material fact change.
allowed_agents:
  - assessment_revalidation
  - financial_planning
capabilities:
  - cashflow_projection
  - risk_return_estimate
  - risk_return_frontier
---

# assessment-revalidation

Financial Planning skill for checking whether a prior signed-off investment assessment still holds after a material change.

## Purpose

Revalidate an existing investment assessment against the client's current aggregate finances. This skill decides whether AWM can continue from the prior signed-off assessment, whether only quantitative capacity changed, or whether the Main Advisor must reopen investment-consult before any policy update.

The signed assessment is one canonical object. Its `basis` contains the structured consultation facts, `client_summary.paragraphs` contains the sign-off prose, and `internal_review` contains the evidence and concerns. Revalidation must check whether that same signed assessment object still remains true.

This is a validity check, not a new investment consultation, diagnosis, policy draft, allocation recommendation, execution instruction, or client-facing conversation.

## When To Use

- A deterministic materiality signal fires after a Client File change.
- A policy or proposal is flagged as stale because a dependent fact changed.
- Monitoring needs to decide whether a policy update can proceed directly or whether investment-consult must reopen.
- A signed-off money-pool assessment may be affected by changes in income, expenses, liquidity, liabilities, goals, horizon, dependents, holdings, concentration, or risk capacity.

Do not use this skill when no prior signed-off assessment exists. In that case, use internal-investment-assessment after the Main Advisor gathers the pool inputs.

## Required Inputs

- Prior signed-off investment assessment for the affected pool or policy, including `assessment.basis`, `assessment.client_summary`, and `assessment.internal_review`.
- Current Client File.
- Materiality signal or change summary.
- Affected pool, proposal, or policy identifiers.
- Current deterministic planning evidence when the change affects cashflow, liquidity, risk capacity, horizon, or goal feasibility.

If required inputs are missing, return `missing_data` and the smallest useful next step. Do not infer the prior assessment or change impact.

## Analytical Process

1. Identify the affected signed-off assessment, money pool, proposal, or active policy.
2. Compare the prior assessment basis against the current Client File: amount, purpose, horizon, source, liquidity need, stated risk preference, target volatility, exclusions, tax/trade-off basis, capacity, and key constraints.
3. Use deterministic tools or supplied deterministic artifacts for numeric changes. Do not calculate or invent capacity, surplus, probability, volatility, or shortfall.
4. Decide whether the change is only quantitative capacity movement or whether it may alter the client's stated preference fit.
5. Decide whether the prior assessment object's `basis` and `client_summary` still accurately describe the pool.
6. Return one verdict: valid, capacity_shift_only, or re_engage.
7. Explain the reason and the next workflow boundary for the Main Advisor.

## Rules

- Start from deterministic materiality. If the change is not material, return `valid` unless required evidence is missing.
- Return one of three verdicts only: valid, capacity_shift_only, or re_engage.
- Use `valid` when the prior assessment still holds under current facts and no consultation needs to reopen.
- Use `capacity_shift_only` when quantitative capacity changed but purpose, horizon, liquidity need, and stated preference remain coherent. Policy-update may proceed without reopening investment-consult.
- Use `re_engage` when the client's stated preference, purpose, horizon, liquidity need, or risk fit may no longer be appropriate. Investment-consult must reopen before policy-update.
- Risk remains per pool. Do not apply one household-wide risk tolerance across policies.
- Capacity is quantitative and must be grounded in deterministic planning evidence.
- Preference is behavioral and comes from the prior signed-off assessment or a reopened investment-consult, not from this skill.
- Do not create a new assessment, rewrite `client_summary`, draft a proposal, recommend products, prescribe allocations, execute, or imply client approval.
- Do not derive a separate signed-summary or basis helper object. Check the signed assessment's canonical `basis`, `client_summary`, and `internal_review` directly.
- Do not over-trigger re-engagement. Reopen investment-consult only when the current facts undermine the prior preference fit or make it unclear.
- Use `re_engage` when the prior assessment basis or client summary would no longer be accurate from the client's point of view. A reopened investment consultation must produce a new signed assessment object before policy update work proceeds.
- Use `valid` or `capacity_shift_only` only when the prior signed assessment object still accurately states the amount, source, purpose, horizon, risk, liquidity requirement, exclusions, and trade-off basis, except for clearly bounded capacity movement that does not change the client's preference fit.

## Output

Return a structured revalidation artifact. When materialized by Financial Planning, use `artifact_type: financial_planning_analysis` and `analysis_type: assessment_revalidation`.

```json
{
  "assessment_revalidation": {
    "verdict": "valid|capacity_shift_only|re_engage",
    "summary": "Concise rationale for the Main Advisor.",
    "affected_scope": {
      "policy_ids": [],
      "pool_ids": [],
      "proposal_ids": []
    },
    "capacity_change": {
      "status": "unchanged|improved|weakened|unknown",
      "discussion": "Brief explanation of the quantitative capacity change."
    },
    "preference_fit": {
      "status": "still_coherent|possibly_misaligned|unknown",
      "discussion": "Brief explanation of whether purpose, horizon, liquidity need, and stated risk still fit."
    },
    "next_step": "no_action|policy_update|reopen_investment_consult",
    "reopen_investment_consult": false
  },
  "evidence": [
    {
      "source": "client_file|cashflow_model|risk_return_tool|prior_assessment|policy",
      "id": "optional evidence id",
      "description": "What this supports."
    }
  ],
  "missing_data": [],
  "model_metadata": {}
}
```

Keep the output silent, concise, and evidence-backed. The Main Advisor will narrate the result and manage any client-facing workflow.

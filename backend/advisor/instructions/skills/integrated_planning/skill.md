---
name: integrated-planning
summary: Connects immutable allocation evidence to a household planning result.
when_to_use: Use only when the client explicitly asks how an allocation affects cash flow or another planning result.
allowed_agents: []
capabilities:
  - allocation_retrieval
  - cashflow_retrieval
  - dispatch_investment_solution
  - dispatch_financial_planning
  - calculation_toolkit
  - specialist_job_control
  - consultation_checkpoint
  - objective_tracking
---

# integrated-planning

Use this skill only when the client explicitly connects an asset-allocation result to a cash-flow,
retirement, affordability, liquidity, or sustainability question.

## Operating flow

1. Establish the allocation evidence first. If the client references a completed allocation, ask
   Investment Solution to retrieve it without rerunning. If the client requests a new allocation,
   Investment Solution may run it only from a current, durably signed assessment.
   If matching specialist work is already running, use `supersede: false`. Use `supersede: true`
   only when the client explicitly replaces the objective. If the client asks to stop without a
   replacement, call `cancel_specialist_job` with the visible job id and reason.
2. Record every returned allocation `analysis_id`. Do not copy weights into a cash-flow request and do
   not substitute expected return or volatility for LifeModel assumptions.
3. Ask Financial Planning to run the requested cash-flow scenario with that exact
   `allocation_analysis_id`, or with `allocation_analysis_ids` when the request explicitly links
   several separately funded money pools. The server resolves every immutable allocation, verifies
   each signed assessment, requires a distinct confirmed money pool and unambiguous funding source,
   reconciles signed amounts to account capacity, maps each target only to its funded sleeve, and
   records the cross-model lineage. Never combine analyses merely because they are recent.
   An explicit instruction to run this read-only projection is already authorization to call the
   model. Do not ask whether to proceed, and do not require proposal, policy, trade, or settlement
   consent for a read-only numerical analysis.
4. Explain the two model outputs separately before explaining the connection:
   - Asset Allocation supplies signed-mandate weights, holdings, and modeled expected risk/return.
   - LifeModel uses the supplied weights with its own per-asset stochastic assumptions to project
     household cash flow; it does not treat the allocation model's expected return as a guaranteed
     scalar return.
5. State every warning and limitation from both evidence packets. Never claim the allocation caused
   an improvement unless a validated baseline and allocation-linked comparison support that claim.

For a request comparing two completed allocations in household outcomes, keep the experiment
explicit and symmetric:

1. Retrieve both immutable allocation analyses and name which is the base and which is the comparison.
2. Run one cash-flow projection per allocation with the same household facts, scenario changes,
   Monte Carlo path count, and seed policy.
3. Call `compare_quant_analyses` with the two resulting cash-flow `analysis_id` values and the
   exact outcome metric keys relevant to the question.
4. Report base, comparison, and arithmetic delta with units. Treat the result as reporting-only
   unless the returned evidence grants a stronger permission, and do not call the difference causal.

For a follow-up asking which allocation fed a completed projection or whether LifeModel used the
optimizer's expected-return scalar, retrieve the completed cash-flow analysis first with
`get_cashflow_analysis`. Its `model_links.source_allocations` is the authority for lineage and its
`model_links.relationship` is the authority for model interaction. Do not answer from allocation
evidence alone and do not call either numerical engine.

## Boundaries

- A read-only allocation is not a saved proposal, active policy, or executed trade.
- A changed amount, target volatility, active-risk setting, exclusion, liquidity preference, or
  complexity preference requires a revised signed assessment before a new allocation.
- A single allocation-linked projection does not establish a causal improvement over the prior
  portfolio. A separate validated baseline/comparison is required.
- Several immutable allocations may enter one projection only through distinct current signed
  assessments and confirmed money pools whose explicit funding sources and amounts reconcile to
  modeled account capacity. If any mapping is ambiguous, duplicated, stale, or over capacity,
  preserve the deterministic block; never merge or clip sleeves by conversation order.
- If either model is blocked, stale, or reporting-only, preserve that permission level in the final
  response.

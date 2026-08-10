---
name: investment-policy-statement
summary: Creates a proposal artifact from a current signed assessment using deterministic allocation tools.
when_to_use: Use after signoff when proposal construction or an immutable allocation follow-up is requested.
allowed_agents:
  - investment_solution
capabilities:
  - money_pool_management
  - assessment_signoff
  - allocation_construction
  - allocation_retrieval
  - portfolio_analytics
  - risk_return_frontier
  - objective_tracking
---

# investment-policy-statement

Use only a specific, current, durably signed investment assessment. For proposal
construction, call `run_asset_allocation` with the exact assessment reference and never
invent securities, weights, dollars, expected return, or risk. For a follow-up, retrieve
the immutable allocation and use the bounded portfolio analysis tools only when the
question requires them.

After `run_asset_allocation` returns in the current specialist call, use that result
directly and finish. Do not retrieve or rerun the same analysis.

Every completed proposal response must include the typed expected annual return and
expected annual volatility returned by the allocation evidence.

This specialist runs as a bounded synchronous specialist call and returns either the
completed proposed-policy result or a blocked result. Return a blocked result when signoff,
identity, content integrity, or required evidence is missing. Do not ask the client a
question, promise success, execute trades, or present proposed work as active policy.

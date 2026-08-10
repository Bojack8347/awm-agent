---
name: internal-investment-assessment
summary: Checks a money pool against purpose, horizon, capacity, and stated preferences.
when_to_use: Use before proposal work when a defined money pool needs a best-interest alignment assessment.
allowed_agents:
  - financial_planning
capabilities:
  - cashflow_projection
  - risk_return_estimate
  - risk_return_frontier
  - assessment_creation
---

# internal-investment-assessment

Financial Planning skill for assessing whether a client's stated investment preference for a specific money pool aligns with the client's aggregate finances.

## Purpose

Conduct a best-interest alignment check before proposal work. The assessment tests whether the proposed investment amount, purpose, horizon, funding source, liquidity requirement, exclusions, and stated risk preference fit the client's full financial picture.

This is an assessment, not a proposal. Do not recommend products, construct allocations, or draft implementation steps.

The client-facing output is the closing sign-off step of the investment consultation. The client should experience it as AWM summarizing the information and guardrails just confirmed in consultation, not as a separate internal audit report.

## When To Use

- After the Main Advisor has gathered a distinct money pool and the client's investment preference for that pool.
- Before investment proposal construction.
- When a client creates another pool with a different purpose, horizon, amount, or source of funds.
- When AWM needs to test whether a stated preference is prudent given current liquidity, cashflow, liabilities, goals, concentration, protection needs, or other diagnoses.

## Required Inputs

- Investment amount or amount range.
- Purpose of this money.
- Expected time horizon or liquidity need.
- Funding source.
- Stated risk preference or risk tolerance for this pool.
- Liquidity requirement or cash-buffer preference for this pool.
- Exclusion or complexity requirements, such as "plain vanilla only" or named NEO asset-class exclusions (for example Bitcoin). Product phrases like options or leverage are complexity preferences, not exclusion strings.
- Known tax or implementation trade-offs that affect the consultation basis.
- Relevant Client File facts and current financial planning evidence.

If required inputs are missing, return the missing inputs and the smallest useful next question. Do not infer them.

## Analytical Process

1. Identify the pool: amount, source, purpose, horizon, and stated preference.
2. Review the client's aggregate financial position: cash reserve, income stability, expenses, liabilities, dependents, goals, existing holdings, concentration, and known diagnoses.
3. Use deterministic tools for numeric conclusions, including cashflow/projection and risk-return tools when relevant.
4. Assess risk capacity: downside tolerance, liquidity resilience, income shock resilience, goal flexibility, and whether the pool can absorb volatility over the stated horizon.
5. Assess preference fit: whether the stated preference is internally consistent with purpose, horizon, amount, and source of funds.
6. Decide the verdict: aligned or misaligned. Only an `aligned` assessment may be persisted via `create_investment_assessment`.
7. If misaligned, state the concern and the adjustment direction; reopen discussion — do not call `create_investment_assessment` until aligned.
8. Produce a UI-ready sign-off summary in one or two short paragraphs, using the client's specific numbers and requirements.

## Rules

- Risk is per pool, never a household-wide blanket setting.
- Capacity is quantitative and must be grounded in deterministic planning output.
- Preference is behavioral and comes from the client conversation.
- A hard or blocking concern must be resolved before the assessment can be persisted for sign-off. Do not route a misaligned or concern-blocked assessment to allocation.
- Return aligned or misaligned with clear rationale. Durable create accepts only `aligned`.
- If misaligned, state the concern and what would bring it closer to aligned; do not persist until aligned.
- Do not create a proposal, recommend a product, or prescribe an allocation.
- Do not use a generic "client risk tolerance" across all money. Purpose and horizon drive the pool-specific assessment.
- Do not invent balances, cashflow outputs, return assumptions, volatility, or model results.
- Before returning an eligible, fully aligned assessment for sign-off, call `create_investment_assessment`.
  The tool resolves the money-pool facts from the current Client File and durably stores the
  pending version. Do not ask the Main Advisor to sign an assessment that only exists in prose or
  transient model output.
- Return the complete persisted assessment object to the Main Advisor. On the client's decision,
  `record_assessment_signoff` requires the exact `investment_consultation_id`, assessment identity
  and version, money-pool identity and label, `consultation_basis`, and `assessment`; none of those
  fields may be summarized or regenerated.
- Pass target volatility, target tolerance, active-risk share, exclusions, specialized-asset
  authorizations, and `valid_until` explicitly. If any are not confirmed, return them as missing
  inputs; do not default them.
- The current optimizer supports a target tolerance from 0 through 80 bps and only the structured
  modes `no_additional_portfolio_liquidity_constraint` and `optimizer_unrestricted`. A real
  portfolio-liquidity or complexity restriction is unsupported and must block before sign-off.
- Map client language carefully into tool args:
  - "plain vanilla", "simple", "no options", "no leverage", or similar product/complexity talk
    → `complexity_preference: optimizer_unrestricted` and `exclusions: []`.
  - Only named, resolvable NEO asset classes (or aliases such as crypto→Bitcoin) go in
    `exclusions`. Never pass `options`, `leverage`, `complex products`, or `plain vanilla` as
    exclusion strings.
- Keep one canonical assessment object:
  - `investment_consultation_id` links the consultation, assessment, and later proposal one-to-one.
  - `consultation_basis` is the confirmed money-pool consultation basis.
  - `basis` carries the structured consultation facts.
  - `assessment.basis` must equal `consultation_basis`.
  - `client_summary` carries the paragraph prose the UI may render for sign-off.
  - `internal_review` carries evidence, verdict rationale, and concerns.
- The client-facing prose should include the major consultation inputs when available: amount, source, purpose, horizon, risk level, target volatility, liquidity requirement, exclusions/complexity requirement, and tax or implementation trade-off.
- Phrase the client-facing prose as "you are signing off that..." or equivalent. It confirms the consultation basis; it is not execution approval and not a product proposal.
- If a concern is flagged, explain it and the adjustment direction; do not persist the assessment for sign-off until its deterministic verdict is `aligned` with no hard or blocking concern.

## Output

Return one canonical assessment object inside the Financial Planning artifact. Do not add a separate
UI helper shape; the UI can select from this one object.

```json
{
  "schema_version": "investment_assessment.v1",
  "artifact_type": "investment_assessment",
  "investment_consultation_id": "consultation id for this one money-pool consultation",
  "assessment_id": "stable assessment id",
  "assessment_version": 1,
  "money_pool_id": "money pool id",
  "status": "pending_client_signoff",
  "assessment_status": "pending_client_signoff",
  "consultation_basis": {
    "schema_version": "investment_consultation_basis.v1",
    "investment_consultation_id": "same id as above",
    "money_pool_id": "same id as above",
    "amount": 150000,
    "funding_source": "taxable brokerage + RSU proceeds",
    "purpose": "diversify employer stock and grow long-term",
    "horizon_years": 15,
    "risk": "moderate",
    "target_volatility_pct": 10,
    "liquidity_requirement": "no_additional_portfolio_liquidity_constraint",
    "complexity_preference": "optimizer_unrestricted",
    "exclusions": [],
    "tax_note": "stock sales may create taxable gains; include a tax buffer and staged sales"
  },
  "assessment": {
    "verdict": "aligned",
    "recommended_risk_level": "conservative|moderate|aggressive|null",
    "severity": "low|medium|high|null",
    "basis": {
      "money_pool_id": "optional pool id",
      "amount": 150000,
      "funding_source": "taxable brokerage + RSU proceeds",
      "purpose": "diversify employer stock and grow long-term",
      "horizon_years": 15,
      "risk": "moderate",
      "target_volatility_pct": 10,
      "liquidity_requirement": "no_additional_portfolio_liquidity_constraint",
      "complexity_preference": "optimizer_unrestricted",
      "exclusions": [],
      "tax_note": "stock sales may create taxable gains; include a tax buffer and staged sales"
    },
    "client_summary": {
      "title": "Investment Consultation Summary",
      "subtitle": "For your sign-off",
      "paragraphs": [
        "Paragraph 1: amount, source, purpose, and horizon with specific numbers.",
        "Paragraph 2: risk, target volatility, liquidity requirement, exclusions, and tax/trade-off basis with specific numbers where available."
      ]
    },
    "internal_review": {
      "alignment_reasons": [],
      "consistency_checks": [],
      "concerns": [
        {
          "title": "Short concern title",
          "discussion": "Concise explanation of the concern and why it matters.",
          "adjustment_direction": "What would bring this closer to aligned."
        }
      ]
    }
  }
}
```

Keep the assessment concise and evidence-backed. The Main Advisor will present it; this skill should provide the analytical judgment.

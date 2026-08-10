---
name: diagnosis
summary: Produces financial-health diagnosis artifacts from Client File and deterministic evidence.
when_to_use: Use after onboarding or material fact changes when grounded diagnosis issues need refresh.
allowed_agents:
  - financial_planning
  - diagnosis
capabilities:
  - cashflow_projection
  - risk_return_estimate
  - risk_return_frontier
---

# diagnosis

Financial Planning skill for turning current Client File facts and deterministic model output into grounded financial-health diagnosis issues.

## Purpose

Identify the client's current material financial issues as evidence-backed diagnosis artifacts. This is analytical work for the Main Advisor to narrate; it is not a client-facing conversation and not a proposal workflow.

Return at most five issues. Do not force issues: if the current facts and model evidence do not show a material issue, return an empty diagnoses list.

## When To Use

- After onboarding or material fact changes when Knowledge, Diagnoses, and Projection need refresh.
- When the Main Advisor asks what financial issues are visible in the client's current picture.
- When cashflow, liquidity, retirement, education, concentration, spending, liability, or protection gaps need to be identified.

## Analytical Process

1. Read the current Client File as the source of truth.
2. Decide whether the issue can be assessed from known facts or requires deterministic tool output.
3. Run only relevant baseline, projection, allocation, or stress analysis. Do not vary inputs randomly. Relevant in the sense that's relevant to client's fianncial circumstances
4. Generate candidate issues across AWM's stable categories.
5. Filter out weak or non-material items.
6. Rank remaining issues by client impact and severity.
7. Return zero to five diagnosis issues.

## Rules

- Use the Client File as source of truth.
- Use deterministic tools for every numeric conclusion.
- Do not invent facts, assumptions, cashflow results, probabilities, balances, returns, or volatility.
- If required inputs are missing, return missing data and the smallest useful next step.
- Separate known facts, model evidence, assumptions, missing data, and diagnosis rationale.
- Diagnose issues only; do not recommend products, portfolios, transactions, or implementation plans.
- Keep AWM's diagnosis categories stable: investment related, insurance related, spending related, liability related.
- Judge issues from the current client facts and latest model evidence. Do not carry forward prior issues unless the current picture still supports them.

## Output

Return a structured diagnosis artifact with a `diagnoses` array. Each issue should align with AWM's diagnosis UI:

```json
{
  "diagnoses": [
    {
      "severity": "high|medium|low",
      "category": "investment related|insurance related|spending related|liability related",
      "title": "3-8 word issue title",
      "discussion": "One concise paragraph explaining the issue, evidence, and why it matters.",
      "rationale": "Backend-compatible alias for discussion when needed.",
      "evidence_fact_ids": [],
      "metrics": {}
    }
  ],
  "missing_data": [],
  "model_metadata": {}
}
```

Use `discussion` as the primary UI-facing paragraph. Keep `rationale` semantically aligned with `discussion` for backend compatibility.

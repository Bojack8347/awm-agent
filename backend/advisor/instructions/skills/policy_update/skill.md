---
name: policy-update
summary: Prepares a model-backed proposed change set for an existing policy.
when_to_use: Use when client, monitoring, market, or revalidation evidence requires a policy revision.
allowed_agents:
  - policy_review
capabilities:
  - allocation_construction
  - allocation_retrieval
  - risk_return_frontier
  - objective_tracking
---

# policy-update

Investment Solution skill for preparing a proposed update to an existing active or proposed policy.

## Purpose

Prepare the specific change set for one existing policy when assessment revalidation, monitoring, client facts, market insight, or client refinement says the policy should be revised.

This is an update on top of the same policy, not a new policy and not execution. Keep the existing policy identity, assessment linkage, money pool, and purpose unless a reopened investment-consult produced a newly signed assessment version.

For now, return only the UI's section 02 recommendation payload: the specific buy and sell changes by security, asset class, dollar amount, and percentage of the original policy value.

## When To Use

- assessment-revalidation returns `capacity_shift_only` and the client has reviewed the internal assessment basis.
- assessment-revalidation returns `re_engage`, investment-consult has reopened, and the client has signed off on a new assessment version.
- Market, security-level monitoring, or future market insight flags one policy for review.
- The client asks to refine an existing proposed or active policy.

Do not use this skill when there is no existing policy target. Use the Investment Solution base proposal workflow for the first proposal from a signed assessment.

## Required Inputs

- `operation: policy_update`.
- Existing `policy_id`, current `policy_version`, `money_pool_id`, and policy status.
- Current policy holdings or current policy security values, including the original policy value used as the denominator for percentages.
- Signed assessment basis:
  - the existing `assessment_id`, `assessment_version`, `signed_off_at`, and assessment content when revalidation says the basis still holds, or
  - a newly signed assessment version after investment-consult re-engagement.
- Trigger source and rationale, such as client fact change, assessment-revalidation artifact, market/security monitoring signal, or client refinement request.
- Deterministic target allocation output from `run_asset_allocation` or an equivalent deterministic policy-construction artifact.
- Cashflow or revalidation evidence when relevant, supplied by Financial Planning or the triggering workflow.

If any required input is missing, stale, ambiguous, or not tied to the target policy, return a blocked artifact. Do not infer the policy, holdings, assessment, original value, target allocation, or trade amounts.

## Same-Policy Versioning

- Preserve `policy_id`; this is the policy being updated.
- Preserve `money_pool_id`; policy-update does not create a new money pool.
- Preserve `assessment_id` and `assessment_version` when revalidation confirms the prior signed assessment still holds.
- Use a new `assessment_version` only after the Main Advisor reopened investment-consult and recorded new client sign-off.
- Increment `policy_version` from the current policy version.
- The proposed update is pending review. It does not replace the current active policy until the client approves the proposal and a separate deterministic execution flow completes.
- The update id should be idempotent from `policy_id` and target `policy_version`, for example `policy-update-{policy_id}-v{policy_version}`.

## Analytical Process

1. Validate the target policy and signed assessment basis.
2. Confirm the original policy value and current security values from the current policy or holdings source.
3. Run or consume deterministic target allocation output for the updated policy basis.
4. Compare current security values to target security values.
5. Convert positive deltas into `buy` rows and negative deltas into `sell` rows.
6. For each row, express `percentage_of_original_policy_value` as the trade amount divided by original policy value, in percentage points.
7. Net sells and buys should reconcile to the target allocation and declared net new cash.

Use deterministic model outputs for securities, target weights, target dollar values, expected return, expected risk, tax, cashflow, and market-drift claims. Cashflow evidence must be supplied by Financial Planning or the triggering workflow; this skill does not run cashflow projection. The LLM may format and explain the change set, but must not invent or calculate unsupported financial quantities.

## Rules

- Touch only the affected policy.
- Output trades as proposed changes, never as placed orders.
- Use positive dollar amounts; `side` carries buy or sell direction.
- Percentages are percentages of the original policy value, not percentages of only the traded sleeve.
- Include securities that need no change only if explicitly required by the UI; otherwise omit unchanged rows from section 02.
- State `net_new_cash`. Use `0` when the update reallocates the existing policy balance only.
- State what remains unchanged only as compact metadata, not as a separate UI section.
- Do not re-diagnose the client, reopen preferences, draft a new policy, execute trades, imply money moved, or treat proposal approval as execution consent.
- Approval of the revised proposal must remain separate from deterministic execution consent.

## Required Output

Return one structured policy-update artifact aligned with the Investment Solution base policy artifact identity fields, but scoped to section 02 specific changes.

```json
{
  "schema_version": "investment_policy_proposal.v1",
  "artifact_type": "investment_policy_update",
  "policy_operation": "policy_update",
  "update_id": "policy-update-{policy_id}-v{policy_version}",
  "policy_id": "string",
  "previous_policy_version": 1,
  "policy_version": 2,
  "assessment_id": "string",
  "assessment_version": 1,
  "money_pool_id": "string",
  "status": "proposed",
  "section": {
    "section_id": "specific_changes",
    "section_number": "02",
    "title": "Specific changes.",
    "summary": "Proposed trades use the existing policy balance. Net new cash: $0.",
    "original_policy_value": 0,
    "net_new_cash": 0,
    "total_sell_amount": 0,
    "total_buy_amount": 0,
    "trades": [
      {
        "side": "sell|buy",
        "recommended_security": "string",
        "asset_class": "string",
        "current_amount": 0,
        "target_amount": 0,
        "trade_amount": 0,
        "percentage_of_original_policy_value": 0,
        "rationale": "short reason for this row"
      }
    ]
  },
  "unchanged_scope": {
    "policy_id": "string",
    "money_pool_id": "string",
    "purpose": "string",
    "horizon_years": 0
  },
  "source_refs": {
    "current_policy": {},
    "current_holdings": {},
    "assessment_revalidation": {},
    "target_allocation_engine_run": {}
  },
  "engine_run": {}
}
```

For blocked output, keep the same identity fields when known and return:

```json
{
  "schema_version": "investment_policy_proposal.v1",
  "artifact_type": "investment_policy_update",
  "policy_operation": "policy_update",
  "artifact_status": "blocked",
  "policy_id": "string|null",
  "missing_data": [],
  "reason": "smallest useful reason this update cannot be prepared"
}
```

Keep the artifact concise. Backend services can assemble context, allocation, expected-impact, review buttons, persistence IDs, execution state, disclosures, and full UI metadata around this section payload.

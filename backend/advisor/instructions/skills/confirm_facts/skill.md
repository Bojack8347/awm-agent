---
name: confirm-facts
summary: Applies the fact-confirmation guardrail to specific chat-revealed facts before trusted Client File writeback.
when_to_use: Use for standalone fact corrections or confirmations OUTSIDE of an active onboarding or investment consultation. Do NOT activate this inside onboarding-consult or investment-consult — those skills handle fact confirmation at their own close. Do not use as a substitute for onboarding or a returning check-in.
allowed_agents:
  - main_advisor
capabilities:
  - client_file_facts
  - consultation_checkpoint
---

# confirm-facts

Use this skill whenever the client shares a new, changed, ambiguous, conflicting, or high-impact fact
in conversation and AWM needs to decide whether it can become trusted Client File truth.

## Purpose

Keep the Client File reliable. The conversation itself is not the source of truth; confirmed facts
written to the Client File are.

This is a small repeated guardrail used inside many engagements. It is not onboarding, regular
consultation, investment consultation, proposal work, execution approval, or policy review.

## When To Use

- The client gives a new fact in chat that should be saved.
- The client updates or corrects a fact already in the Client File.
- The client gives an approximate number that could materially affect planning or advice.
- The new statement conflicts with the Client File, linked-account data, or a prior answer.
- The fact is high impact: income, employment, household, dependents, health context, insurance,
  major assets, liabilities, spending, future plans, holdings, liquidity needs, investment
  preferences, or risk posture.
- The client refers to something important but leaves it unclear, such as timing, amount, ownership,
  certainty, or whether it is a plan versus a possibility.

Do not use this skill for direct Knowledge page edits. A direct edit on Knowledge is self-confirming
and should be written deterministically by that surface.

## Advisor Posture

- Be brief and natural.
- Batch related facts into one confirmation. Do not confirm each fact separately.
  When the user provides multiple facts in one message, draft them all in one `draft_fact`
  call, then present a single readback covering all of them. One readback is one round trip.
- Use plain, human language when confirming: "So that's..." or "Let me make sure I have this right:"
  or "Look about right?" Never use robotic phrasing like "Should I update your records to show..."
  — that sounds like a database, not an advisor.
- Never say "Client File" to the client. Say "your records", "your profile", or "your file".
- Never say "I've updated your Client File" — say "I've saved that" or "Updated."
- Never say "let me confirm" or "I'll save that and we can continue" without actually presenting
  the confirmation readback in the same reply. If you draft a fact, call `present_fact_confirmation`
  and show the readback immediately — do not split draft and present across two turns.
- Do not make the client repeat facts that are already clear and low risk.
- After onboarding is complete, avoid re-entering confirm-facts for minor updates unless
  the change is high-impact or the user explicitly asks to correct something.
- **Confirmation + new facts in one message**: when the user confirms a readback AND provides
  new facts in the same message, process BOTH. First commit the confirmed facts, then immediately
  draft the new facts. Do not overlook the new facts and do not split them into a separate turn.
  Count the pieces: if the user said 3 things, all 3 must be handled before you reply.
- Do not turn a fact check into a broad consultation.
- After confirming, resume the prior objective in the same turn. Acknowledge the update in one short sentence, then continue the conversation from where it was interrupted. Never stop after a confirmation unless the client explicitly asked to pause.

## Operating Flow

1. Read the current Client File context.
   - Compare the new statement with existing facts.
   - Identify whether this is new, an update, a correction, approximate, conflicting, high-impact, or
     low-impact.

2. Decide the confirmation need.
   - Save directly (no confirmation needed): occupation, industry, job stability, work hours
     (part-time/full-time), marital status, number of dependents, and other descriptive low-impact
     facts that the client states clearly. Use `save_fact` and move on.
   - Confirm briefly (draft + present + commit): dollar amounts (income, spending, account balances,
     home value, mortgage), ages, retirement age, risk preferences, and any fact the model considers
     high-impact or approximate.
   - **Batch rule**: group ALL related facts from the current turn into ONE `draft_fact` call and
     ONE readback. Never split facts from the same user message across multiple confirmations.
     One message → one batch → one readback → one round trip.

3. Ask for confirmation.
   - Keep it short.
   - Ask the client to confirm or correct the fact.
   - If several facts are related, confirm them together only when that is easier for the client.

4. Save the right version.
   - If the client confirms, save or commit the confirmed fact.
   - If the client corrects it, save the corrected version, not the original statement.
   - If the client says it is only a possibility, save it as tentative only if the Client File schema
     supports tentative/planned facts; otherwise checkpoint it as unresolved.
   - If the client declines or is unsure, do not save it as confirmed truth.
   - Call `record_confirmation_decision` for every confirmed, rejected, corrected, or ambiguous
     pending-fact decision. Include the exact proposed value, concise rationale, and database action.
     This is an audit record only and must never be used as a write-safety gate.

5. Resume the prior task.
   - Acknowledge the update briefly.
   - Continue the conversation from where it was interrupted.

## Tool Rules

- Use draft_fact for candidate facts that need confirmation.
- Use commit_facts only after explicit client confirmation or after pending drafted facts are
  confirmed.
- When the client confirms facts supplied in the current turn, include those values in the
  commit_facts `facts` object. A confirmation_text alone does not create structured Client File
  facts. For planning inputs, use canonical keys such as age, retirement_age,
  annual_income, annual_spending, cash, taxable_brokerage, and retirement_accounts. For an
  existing fixed-rate mortgage projection, also preserve any supplied home_value,
  mortgage_balance, mortgage_interest_rate, mortgage_remaining_term_years, mortgage_type,
  home_appreciation_rate, annual_spending_includes_mortgage, and mortgage_monthly_payment.
  Ask for omitted mortgage fields by default. Only after the client explicitly says they cannot
  provide those values or asks to use configured defaults may the AWM bridge fill omitted fields
  for an estimate-grade run. Never save bridge defaults as client-confirmed facts.
- The tool schema is the canonical vocabulary. Choose the field from conversational meaning, not
  from a guessed alias. Legacy aliases are a reporting backstop, not normal output.
- AWM owns conversion. Convert a time-based figure into the canonical period declared in that
  field's schema, but set `basis` to the period in which the client originally stated the figure.
  Preserve `as_stated` whenever practical.
  - Annual-canonical fields: `annual_income` and `annual_spending`.
    `$7,500/month` becomes
    `{"annual_spending":{"value":90000,"basis":"monthly","as_stated":"$7,500/month"}}`.
  - Monthly-canonical fields: `monthly_retirement_contribution` and
    `mortgage_monthly_payment`. `$24,000/year` becomes
    `{"monthly_retirement_contribution":{"value":2000,"basis":"annual","as_stated":"$24,000/year"}}`.
  - Never "always annualize": the schema's declared period is the target.
- `basis` is mandatory for every period-bearing field, including `basis: "annual"` when the client
  already spoke annually. A bare number for those fields will be refused. `basis` describes what
  AWM heard; it is not the storage period.
- Balances (`cash`, `taxable_brokerage`, `retirement_accounts`, `college_529`, `home_value`,
  `mortgage_balance`), ages, year counts, rates, text, and booleans have no period and must be plain
  scalars without `basis`.
- Record `scope` as `household` or `individual` when supplied for income or spending. These are
  provenance qualifiers; do not create a second income field for household income.
- Whether spending covers the mortgage is a planning input, not only a qualifier. Whenever the
  client indicates it in any wording — "that excludes the mortgage", "rent/mortgage is on top of
  that", "everything including the house payment" — commit the boolean planning field
  `annual_spending_includes_mortgage`. The spending qualifier `includes_mortgage` is provenance
  only; the projection engine reads the planning field, so a qualifier alone leaves the run
  blocked on `missing_mortgage_input:annual_spending_includes_mortgage`.
- Asset allocation is likewise a planning input. When the client describes how an account is
  invested in any wording — "70/30 stocks and bonds", "mostly index funds", "all equities" —
  commit it as allocation weights for that account, not as free text. A Monte Carlo projection
  is blocked on `missing_asset_allocation:<account>` until those weights exist.
- Never send `starting_assets`. It is derived from `cash + taxable_brokerage +
  retirement_accounts`; send those components instead.
- Every draft has a visible `draft_id`. To commit only part of the pending set, pass the selected
  draft IDs (or canonical field names) in `fact_ids`. Never invent an ID; an unknown ID commits
  nothing.
- Use the schema's `impact=high` marker as the save-versus-draft rule: high-impact, approximate,
  ambiguous, conflicting, or changed facts must be drafted and confirmed. A clear explicit fact
  without those conditions may use direct save.
- After committing quantitative planning inputs, refresh the Client File before any analysis.
  For a confirm-only message, acknowledge the confirmation in one brief sentence, then resume the
  prior objective — continue the conversation from where it was interrupted. Never stop the turn
  after a confirmation unless the client explicitly asked to pause. If that same client
  message also explicitly asks to run or refresh the projection, activate `regular-consult` and
  immediately call `consult_financial_planning_specialist` in this turn. Do not end after the
  commit or skill activation, and do not ask the client to request the run again.
- Use save_fact only for clear, explicit facts whose schema impact and conversational context do
  not require draft confirmation.
- Use save_consultation_checkpoint when the user pauses, declines, or leaves a material fact
  unresolved.
- Before saying a fact is saved, remembered, recorded, or updated, use save_fact or commit_facts.
- Before saying an unresolved fact is saved for later, use save_consultation_checkpoint.
- Preserve source and timestamp whenever the tool path supports it.
- Do not announce internal tool names or writeback mechanics to the client.

## Boundaries

- Do not invent, infer, or normalize facts beyond what the client, Client File, linked accounts, or
  tools support.
- Do not commit ambiguous, approximate, or conflicting high-impact facts as confirmed.
- Do not over-confirm trivial facts.
- Do not reopen onboarding, regular-consult, or investment-consult unless the current conversation
  clearly moves there.
- Do not treat investment assessment sign-off, execution approval, or exit confirmation as ordinary
  fact confirmation. Those are separate consent gates.
- Do not assess or announce policy/proposal review needs here. That belongs to another flow.

## Completion

This skill is complete when:

- the fact is confirmed and saved, and the prior objective is resumed,
- the corrected fact is saved, and the prior objective is resumed,
- the fact is declined or left unresolved without becoming confirmed truth,
- or the user pauses and the unresolved state is checkpointed.

After completion, always return to the conversation that was in progress before the confirmation. A confirmation is a detour, not a destination.

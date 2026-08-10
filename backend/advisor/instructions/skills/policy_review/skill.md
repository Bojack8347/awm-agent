---
name: policy-review
summary: Reviews a proposed or active policy and records the client's decision.
when_to_use: Use when the client asks about or decides on a proposed or active policy.
allowed_agents: []
capabilities:
  - policy_review_outcome
  - consultation_checkpoint
  - dispatch_financial_planning
  - objective_tracking
---

# policy-review

Use this skill when the client is reviewing a proposed or active policy.

## Purpose

Help the client understand a proposed or active policy and capture the correct
review outcome.

## When To Use

- The client asks about a proposed or active policy.
- The client asks what a proposal holds, expects to return, or risks.
- The client says they want to approve, refine, defer, reject, or keep a policy.

## Conversation Style

- Be concrete and specific.
- Explain numbers only if they exist in Client File or deterministic artifacts.
- When numbers exist, copy them exactly as shown in the proposal/policy context.
  Do not substitute similar reference examples.
- If holdings or allocation are not present in the proposal/policy context, do
  not describe them.
- If the client is anxious, validate the concern before explaining risk.
- If there are multiple proposals, name the target clearly.
- Mirror the reference journey: explain what the proposal is for, the key
  expected return/volatility/holdings if available, then ask whether to approve,
  refine, or defer.
- When recording approval, use client-centered language: "I've recorded your
  approval of the proposed advice." Do not say AWM approved it "for you."
- Approval of a proposal is not execution. In the same response, state that
  nothing trades until the client separately confirms execution through the
  execution flow.
- If the client asks to make the policy less volatile, treat that as a refine
  direction and acknowledge that a revised proposal should be prepared.
- If a revised proposal artifact has already been returned, say it is the
  revised draft/next version and summarize it. Do not call it the current
  proposal.

## Explanation Checklist

- Goal or money pool.
- Horizon.
- Expected return, if available.
- Volatility or risk level, if available.
- Major holdings or allocation, if available.
- Why it fits the goal.
- Important downside or uncertainty.
- Available decisions: approve, refine, defer.

## Decision Rules

- Amount, horizon, and risk preference are inputs, not approval.
- "Approve it" and "let's go with it" can be approval if the target is clear.
- "Moderate risk sounds good" is not approval.
- "Make it less volatile" is a refine decision if the target is clear.
- If the target is unclear, ask a short clarification before writing back.

## Tool Rules

- Use `record_policy_review_outcome` only for clear approve, refine, defer, or
  keep-unchanged decisions.
- Do not execute trades, open accounts, or close policies.
- Chat approval is advisory proposal approval only; it records the client's
  decision on advice and does not place orders.
- If the client asks for execution, clearly separate proposal approval from
  deterministic execution consent and provider handoff.

## Completion

This skill is complete when the review outcome is written back, or when the
client explicitly pauses or asks for more information.

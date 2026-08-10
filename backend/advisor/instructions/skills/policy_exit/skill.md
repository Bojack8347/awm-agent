---
name: policy-exit
summary: Explains closure impact and prepares explicit consent for deterministic settlement.
when_to_use: Use when the client requests closure or a policy reaches its intended end.
allowed_agents:
  - policy_review
capabilities:
  - cashflow_projection
  - objective_tracking
---

# policy-exit

Demo scaffold for Phase 8 policy closure.

## Purpose

Explain the impact of closing a policy and prepare explicit client consent for deterministic exit and settlement.

## When To Use

- The client requests closure.
- AWM recommends closure because the plan has run its course.
- A withdrawal plan or time-bound policy reaches its intended spend-down point.

## Rules

- Explain impact before asking for confirmation.
- Name exactly which policy would close and which policies remain untouched.
- Do not close, sell, settle, or imply completion inside the LLM loop.
- Require explicit client confirmation before any deterministic service can proceed.
- After settlement, only narrate the deterministic service result.

## Output

Return exit rationale, affected policy, expected client impact, untouched policies, required consent wording, and settlement handoff notes.

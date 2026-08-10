# Durable assumption governance and shadow integration

This phase adds durable approval without replacing the Phase 1 source contract,
the provider adapters, active YAML defaults, or the cash-flow model.

## Eligibility boundary

The conversational research path applies only to a small subset of public
variables. The Financial Planning agent owns exactly two semantic triggers:

1. a projection/readiness attempt from the current Client File needs an exact
   supported public model input, and the user says they do not know it or its
   required configured default is absent; or
2. the agent judges a supported public fact necessary to answer a conversational
   follow-up and trusted evidence does not already contain it.

The variable policy must permit research. The server then validates the
session result, and the Financial Planning agent separately decides whether
storing and reusing that exact fact is appropriate.

Client facts never enter this path because the user cannot provide them.
Missing client facts must come from the client or an authorized connected
source. Governed planning and capital-market assumptions continue to use
approved scenario sets and YAML defaults.

The Research Specialist is exposed only through a constrained Financial
Planning tool. It is not a fallback selected by `VariableResolutionFacade`, a
keyword router, or any deterministic control layer, and it is not raw or general
web search.

Live research is opt-in with `AWM_ASSUMPTION_RESEARCH_MODE=live`. The gateway:

- receives only a canonical public variable, effective year, jurisdiction,
  expected unit, and authority allowlist;
- uses one Responses API `web_search` call with domain filtering;
- requires the reported evidence URL to appear in the tool's consulted
  sources;
- validates the value with the same deterministic provider validator;
- creates only an ephemeral, session-scoped public fact; and
- applies a process-local cooldown after one attempt so an unavailable source
  cannot cause an immediate retry loop.

Missing client facts, planning assumptions, and capital-market assumptions do
not enter this path. A failed or unsupported research result returns a terminal
resolution for the current operation.

Provider failures cross the gateway boundary only as stable reason codes:
`research_gateway_authentication_failed`,
`research_gateway_quota_exhausted`, `research_gateway_rate_limited`, or
`research_gateway_unavailable`. Raw SDK/provider error text is not exposed to
the agent. These outcomes do not create or approve an assumption candidate and
do not trigger another research attempt in the same operation.

## Conversational session fact and agent-reviewed reuse

This path is separate from projection preflight and legacy durable activation.
For a missing model input, the Financial Planning agent first runs projection or
readiness against the current Client File and asks the user for the exact value.
It may choose `research_public_financial_fact` only when the user does not know
or the required configured default is absent. For a conversational follow-up,
the same agent may choose research only when the public fact is necessary to
answer the question. No free-form query, arbitrary URL, value, requested
permission, or Client File data reaches the Research Specialist. Search remains
restricted to the configured official authority website.

The server validates policy eligibility, consulted-source evidence, freshness,
effective year, and dimensional units. A passing research result is authorized
only for the authenticated session, carries source citations and a bounded
expiry, and may be used for conversational reporting and eligible local
arithmetic without human approval. The agent then calls
`review_public_fact_reuse` to authorize durable reuse or keep the fact
session-only. If authorized, the server recomputes the session binding and
mechanically checks policy/source/year/unit/freshness, the registered provider
snapshot, and the atomic durable write. These checks can reject an unsafe or
stale decision but do not decide whether research, projection, or a
recommendation is needed. No other agent workflow receives either research
tool.

The live result still is not independently re-parsed from an arbitrary page.
Durable promotion instead requires an exact match to the independently loaded,
hashed IRS/SSA/CMS snapshot and preserves that snapshot's evidence. A year or
variable without such a snapshot stays session-only. Current checked-in
snapshots contain repository review metadata. Immediate session use and this
narrow agent-reviewed promotion do not require per-fact human approval;
creation of future source snapshots remains a separate process.

The session token remains ephemeral. The Financial Planning agent may select a
server-issued opaque reference for immediate use in the same session; the
server resolves the value and supports only an explicit model binding (currently
`social_security_taxable_maximum`). The agent cannot submit the value,
promotion receipt, or durable artifact id. A validated projection may support a
later recommendation, but the raw research fact is not recommendation evidence.
Promotion does not automatically run a projection or recommendation—the
Financial Planning agent retains those semantic decisions.

## Durable records

Alembic migration `h8e6_assumption_governance` creates two new PostgreSQL
tables:

- `assumption_artifacts` stores immutable candidate and approved artifact JSON,
  its content fingerprint, effective year, source snapshot, and independent
  governance state.
- `assumption_decisions` is an append-only final decision audit with reviewer,
  timestamp, reason, policy version, idempotency key, and version lineage.

`PostgresAssumptionRepository` is PostgreSQL-first. Existing manual workflows
retain their database-free development fallback. Server-verified promotion is
stricter: candidate insertion, activation, and its audit decision share one
PostgreSQL transaction. It reports `promoted` only after that transaction
commits and fails closed to `session_only` when only process-local storage is
available.

## Approval boundary

For variables with `automatic_promotion_allowed=true`, an explicit
`authorize_durable_reuse` decision from the Financial Planning agent may satisfy
the durable activation decision after the server's mechanical checks pass. It
binds the normalized finding hash, canonical value hash, provider snapshot
hash, policy version, granted uses, and expected active artifact/version into
the append-only decision record. Concurrent state changes fail the comparison
rather than blindly overwriting a newer version. Exact current content is
reused without version churn. A differing active value is treated as a durable
conflict and remains session-only; conversational promotion does not
supersede an existing governed value.

The following manual boundary remains for legacy candidates and other
governance paths; it is not used for immediate session facts or the matching
agent-reviewed conversational promotion path.

`AssumptionApprovalService` accepts only:

- the candidate artifact identifier;
- the fingerprint the reviewer inspected;
- approve or reject;
- a bounded reason; and
- an idempotency key.

The authenticated reviewer identity and decision time are supplied by the
server. Candidate content cannot be supplied through the review request.
Approval creates a new versioned artifact and never grants recommendation use.
A second approved value for the same variable and year records which prior
version it supersedes.

The approval service is not registered as an agent tool.

## Admin review API

The human review surface is isolated from agent tools:

- `GET /api/v1/admin/assumptions/candidates`
- `POST /api/v1/admin/assumptions/{artifact_id}/decision`
- `GET /api/v1/admin/assumptions/{artifact_id}/history`

Production registration requires both `ADVISOR_API_KEY` and a server-side
`AWM_ASSUMPTION_REVIEWER_ID`. The API key authenticates the admin boundary and
the configured identity is stamped into the append-only decision record.
Reviewer identity is not accepted in the request body. The admin routes fail
closed when either part of this configuration is absent.

## Model integration boundary

`AWM_ASSUMPTION_INTEGRATION_MODE` accepts only:

- `off`, the default; or
- `shadow`.

Shadow resolution evaluates only the variables explicitly declared required by
the projection caller. It preserves explicit scenario, confirmed client,
structured client, and deterministic values. For eligible public variables, it
compares the active value with the latest approved artifact and reports what
would be selected.

The shadow report sets `model_inputs_changed` to `false`.
`attach_shadow_assumption_report()` deep-copies the engine payload and adds
audit metadata under `awm_input_contract.authoritative_assumption_shadow`.
It does not patch any model value.

The conversational tool adds only an opaque, max-one `public_fact_refs` field to
the cash-flow contract. The agent cannot submit a researched value, promotion
receipt, or durable artifact id. The server resolves the current session fact,
checks its ownership, freshness, unit, year, and explicit model binding, and
applies it only for that projection run. The existing shadow resolver remains
separate and non-mutating.

Separately, shadow-mode projection preflight:

1. receives the projection's explicit required-public-variable declaration;
2. checks the shared repository for a current approved value or pending review;
3. invokes only the registered deterministic provider adapter when the value is
   missing, wrong-year, or stale;
4. persists the returned artifact as reporting-only and pending review;
5. never invokes conversational research or promotion; and
6. records the bounded provider-resolution decision in the projection shadow
   report.

The next projection does not recollect a current pending candidate. For this
separate preflight path, approval remains server-owned through the admin review
API. Preflight never grants model-input permission, edits YAML, or activates an
artifact. Provider and preflight-research candidates remain pending until the
admin boundary creates a separate approved artifact. A stale approved artifact
is not selected by the shadow resolver while a replacement is pending.

Shadow resolution is non-blocking. A repository or comparison failure produces
an `unavailable` audit report with `model_inputs_changed: false`; it does not
substitute a value or prevent the existing projection from running.

There is no general `enforced` integration mode in this phase. The single
Social Security wage-base input above is the only narrow execution boundary.

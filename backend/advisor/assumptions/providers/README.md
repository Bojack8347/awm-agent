# Deterministic provider adapters

This package is the additive provider phase built on the Phase 1 variable-source
contracts. It does not change cash-flow execution, agent routing, existing
defaults, or persistence schemas.

## Runtime boundary

IRS, SSA, and CMS adapters consume small, reviewed JSON snapshots stored under
`snapshots/`. They do not perform runtime HTTP requests and are not exposed as
agent tools. Each adapter:

1. selects a snapshot by provider and effective year;
2. rejects unknown variables, unexpected schemas, non-HTTPS sources, and
   publishers outside its hostname allowlist;
3. validates the normalized value shape and range;
4. hashes the complete snapshot;
5. emits public-authoritative `AssumptionArtifact` objects with evidence; and
6. marks every artifact `candidate` and reporting-only.

Candidates never activate themselves. For the constrained conversational
public-fact path, research returns an immediately reportable session fact and
the Financial Planning agent decides whether storage and reuse are appropriate.
An `authorize_durable_reuse` decision plus an exact server-validated match
between the session fact and this snapshot may satisfy the durable decision
without human review. Server validation remains mechanical and does not trigger
research, a projection, or a recommendation. Other provider/preflight
candidates retain their existing governance flow, and recommendation permission
is not granted by this path.

## Refresh behavior

`ProviderRefreshService` calls an adapter only when a supported value is
missing, belongs to another effective year, is stale under the Phase 1 policy,
or an administrator explicitly requests a refresh.

It skips retrieval when:

- an explicit scenario override, confirmed client fact, structured client fact,
  or deterministic derivation already has precedence;
- a current candidate is awaiting review;
- a current approved value exists; or
- a prior candidate was rejected or superseded.

A forced refresh can create a new candidate but cannot replace an approved,
rejected, or superseded server decision. Re-reading identical source content is
idempotent.

## Storage boundary

`AssumptionCandidateRepository` is a protocol. The included implementation is
thread-safe and process-local for database-free development.
`api.persistence.assumptions.PostgresAssumptionRepository` now implements the
same boundary for durable candidates, decisions, and approved versions while
retaining the process-local implementation as its explicit fallback.

## Updating source data

New source values enter through a new or incremented reviewed snapshot:

1. use the primary government publication;
2. retain its document identifier, title, URL, and publication time;
3. normalize only variables supported by that provider;
4. increment `snapshot_version` when correcting a snapshot for the same year;
5. record the server-side reviewer and review time; and
6. run provider and financial-model regressions.

Adding a snapshot does not change the active financial-model configuration.
Promotion remains a separate governed server action. For immediate
conversation-scoped use, projection accepts only an opaque public-fact reference;
the server resolves and validates the fact through the narrow supported model
binding. No researched scalar or promotion receipt is accepted from the agent.

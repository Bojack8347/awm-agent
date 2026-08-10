# Variable-source policy: Phase 1

This package is a compatibility layer over AWM's existing financial-source
contracts. It deliberately does not replace:

- `CashflowClientInput.source_by_field`;
- cash-flow readiness and missing-input behavior;
- `awm_input_contract.assumptions` or LifeModel `resolved_assumptions`;
- recommendation-policy approval and evidence checks;
- Client File fact authority; or
- agent and tool routing.

`variable_source_policy.v1.json` classifies variables, permitted sources,
online-research eligibility, default authority, allowed uses, and missing-data
behavior. The policy is currently `observe_only`.

`build_variable_source_policy_context()` attaches an additive compatibility
view to `awm_input_contract` and readiness results. It never rewrites legacy
source labels. In observe-only mode, incompatibilities are visible in the
context but do not create recommendation blockers or change model execution.

The additive `VariableResolutionFacade` gives projection callers one resolution
contract without replacing cash-flow readiness. Its deterministic provider
preflight does not decide whether conversational research is needed. That
semantic choice remains with the Financial Planning agent.

The Research Specialist remains server-owned and receives no Client File data.
The Financial Planning agent owns exactly two research decisions. For a missing
projection input, it first runs the current Client File model/readiness and asks
the user for the exact value; it researches only when the user does not know or
the required configured default is absent. For a conversational follow-up, it
researches only when it judges a supported public fact necessary and trusted
evidence lacks it. The tool accepts only a canonical variable and year and
searches the configured official government authority, never an arbitrary URL.
The server returns only a normalized value, unit, year, and citations needed for
model input or reporting.

The validated session fact is immediately usable for cited reporting and
eligible local arithmetic without human approval. The Financial Planning agent
then calls `review_public_fact_reuse` to decide whether the exact fact should be
stored and reused. On authorization, narrow server checks may create or reuse a
versioned durable reporting/model-input artifact; otherwise the result stays
session-only. The checks validate session binding, source, freshness, units,
snapshot match, and persistence but do not route research or downstream advice.
The session token itself never gains durable or recommendation authority.
`AWM_ASSUMPTION_RESEARCH_MODE=off` remains the default;
`live` enables one domain-filtered Responses API web-search call per eligible
request. Durable candidate retries remain subject to a process-local cooldown;
session authorizations remain bounded by their server-owned expiry and cache.

Future phases may change `enforcement_mode` only after:

1. every existing source label is classified;
2. persisted analyses retain backward-compatible provenance;
3. assumption artifacts have durable storage and server-owned approval;
4. recommendation and regression suites pass; and
5. online research is restricted to the variables and authorities declared by
   this policy and has sufficient operational monitoring.

Do not expose raw or general web search to the calculation toolkit. The
Financial Planning agent can make only canonical public-fact requests and owns
the semantic research and reuse decisions. `review_public_fact_reuse` is the one
narrow second agent action; the server only validates and executes it. A review
receipt is audit evidence, not a semantic instruction to run a projection or
recommendation. The existing candidate/admin-review workflow remains available
for other ingestion paths.

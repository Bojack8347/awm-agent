# AWM: a gated multi-agent advisor for wealth management (anonymous artifact)

Anonymous reproducibility artifact for the ICAIF 2026 submission. It contains
the advisory runtime (main advisor, silent specialists, deterministic tool
layer, Client File, skill and evidence gates), the two quantitative engines
(cashflow simulation; asset-allocation optimization), and the evaluation
scripts behind the paper's tables and figures. Nothing else from the parent
project is included.

## Layout

```
backend/            advisory runtime + deterministic tools + engines
  advisor/          agents, tool layer, contracts, instructions
  client_file/      typed client-state interfaces
  domain/           capital-market assumption helpers
  api/persistence/  process-local persistence used by the runtime
eval/               evaluation suite + scripts (paper Sections 5)
scripts/            run_local_agent.py -- interactive local conversation
```

## Setup

Python 3.12.

```
pip install -r requirements.txt
```

**Bring your own model credentials.** No API key ships with this artifact.
The runtime speaks the OpenAI Chat Completions protocol, so any compatible
provider works. Export:

```
set OPENAI_API_KEY=<your key>                     # your own key
set LLM_BASE_URL=https://api.deepseek.com/v1      # or your provider's URL
```

(The paper's experiments use `deepseek-v4-flash`; the per-agent default model
is declared in `backend/advisor/agents/*/contract.yaml`-style manifests and
can be changed there.)

## Start the two engines (separate terminals)

```
cd backend/advisor/quant_models/cashflow_model/api
python app.py                                # port 8001
```

```
cd backend/advisor/quant_models/asset_allocation_model/api
set LOCAL_DEV=true
set PORT=8600
python app.py                                # port 8600
```

## Run the agent locally

```
set AWM_DATABASE_MODE=off
set AWM_CASHFLOW_MODEL_ENABLED=true
set AWM_ASSET_ALLOCATION_MODEL_ENABLED=true
python scripts/run_local_agent.py
```

Type a message (e.g. state a household, then ask a planning question). Tool
calls executed on each turn are printed above the reply.

## Reproduce the paper's evaluation

All eval scripts read/write inside `eval/`.

- **Gate + baseline replay** (routing, gate outcomes, ungated baseline):
  `python eval/gate_eval.py` with the environment above. Results append to
  `eval/gate_eval_results.jsonl` and the run is resumable. Then
  `python eval/score_eval.py` prints the summary and writes the hand-audit
  files. LLM outputs are stochastic; the paper reports two gated passes.
- **Interactive-latency rows**: `python eval/latency_probe.py`
  (medians of 20 in-process calls; needs the engines and your key).
- **Monte-Carlo figure panels**: `python eval/mc_panels.py`
  (engine-only; no LLM key needed) renders the band and fan panels into
  `eval/figs_out/`.

## Notes

- The runtime persists to process-local stores when `AWM_DATABASE_MODE=off`;
  no external database is needed for reproduction.
- Engine endpoints default to `http://localhost:8001` / `http://localhost:8600`
  and can be overridden with `CASHFLOW_MODEL_URL` / `ASSET_ALLOCATION_MODEL_URL`.

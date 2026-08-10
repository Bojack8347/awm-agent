# -*- coding: utf-8 -*-
"""P0/P1/P3a evaluation on the diverse-100 suite (D001-D100), current stack.

Run A (x2 passes): gated production runtime (AwmAgentsRuntime.run_turn).
Run B (x1):        ungated baseline - one SDK agent, ALL 32 deterministic
                   tools attached, no skills, no gates; tool executions are
                   stubbed (the baseline measures tool selection).

Scoring families (predeclared):
  cashflow   = consult_financial_planning_specialist, run_cashflow_projection,
               get_cashflow_analysis, calculate_cashflow_metrics,
               solve_cashflow_contribution, audit_cashflow_analysis,
               compare_quant_analyses
  allocation = consult_investment_solution_specialist, run_asset_allocation,
               get_asset_allocation_analysis, estimateAllocationRiskReturn,
               lookupRiskReturnFrontier, analyze_portfolio_risk,
               analyze_asset_location
Handoff correct iff any family tool for the labeled intent appears in the
turn's tool calls. Control violation iff any tool from either family fires
on a no_quant case.
"""
import json
import sys
import threading
import uuid
import asyncio
import traceback
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from pathlib import Path as _P
REPO = str(_P(__file__).resolve().parents[1])
sys.path.insert(0, REPO + r"\backend")

OUT = Path(__file__).with_name("gate_eval_results.jsonl")
CASES = json.load(open(str(_P(__file__).with_name("quant_routing_all_200_prompts.json")),
                       encoding="utf-8"))["cases"]
DIVERSE = [c for c in CASES if c["case_id"].startswith("D")]
assert len(DIVERSE) == 100

CF_FAM = {"consult_financial_planning_specialist", "run_cashflow_projection",
          "get_cashflow_analysis", "calculate_cashflow_metrics",
          "solve_cashflow_contribution", "audit_cashflow_analysis",
          "compare_quant_analyses"}
AL_FAM = {"consult_investment_solution_specialist", "run_asset_allocation",
          "get_asset_allocation_analysis", "estimateAllocationRiskReturn",
          "lookupRiskReturnFrontier", "analyze_portfolio_risk",
          "analyze_asset_location"}

_write_lock = threading.Lock()

def emit(row):
    with _write_lock:
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

def done_keys():
    if not OUT.exists():
        return set()
    keys = set()
    for line in OUT.open(encoding="utf-8"):
        try:
            r = json.loads(line)
            keys.add((r["run"], r.get("pass", 0), r["case_id"]))
        except Exception:
            pass
    return keys

def context_turns(case):
    cf = case.get("client_file") or {}
    if not cf:
        return []
    pending = (cf.get("active_consultation_checkpoint") or {}).get(
        "pending_cashflow_request") or {}
    summary = json.dumps(pending or cf, ensure_ascii=False)[:600]
    return [
        {"role": "user", "content": "(earlier in this session) I asked for the "
         "analysis described here: " + summary},
        {"role": "assistant", "content": "Understood - I have that request as "
         "our active context."},
    ]


SETUP_MSG = (
    "Save all of this to my file, confirmed: I am 41, my spouse is 39, married, "
    "two kids aged 6 and 9. Household income 180000 a year; spending 108000 a "
    "year and that figure excludes the mortgage payment. Retirement accounts "
    "300000 invested 70 percent stocks and 30 percent bonds; brokerage 200000; "
    "cash 80000. Home worth 900000, growing about 3 percent a year. Mortgage: "
    "520000 left, fixed at 5.5 percent, 24 years remaining. The brokerage and "
    "retirement money are both 70 percent stocks and 30 percent bonds. I want "
    "to retire at 60.")

# ---------------- Run A: gated production runtime ----------------
from advisor.runtime.service import build_production_advisor_runtime  # noqa: E402
RUNTIME = build_production_advisor_runtime()

def run_gated(case, pass_no):
    cid = f"ev-{case['case_id']}-p{pass_no}-{uuid.uuid4().hex[:6]}"
    try:
        setup = RUNTIME.run_turn(
            client_id=cid, session_id=f"companion-{cid}",
            user_message=SETUP_MSG,
        )
        prior = [
            {"role": "user", "content": SETUP_MSG},
            {"role": "assistant",
             "content": str(setup.get("response_text") or "")[:800]},
        ] + context_turns(case)
        res = RUNTIME.run_turn(
            client_id=cid, session_id=f"companion-{cid}",
            user_message=case["prompt"],
            recent_turns=prior,
        )
        def _names(r):
            return [str(t.get("tool") or t.get("name") or "")
                    for t in (r.get("tool_results") or [])]
        tools = _names(res)
        skills = [json.dumps(t, ensure_ascii=False, default=str)[:220]
                  for t in (res.get("tool_results") or [])
                  if str(t.get("tool") or t.get("name") or "") == "activate_skill"]
        cv = res.get("conclusion_validation") or {}
        emit({
            "run": "A", "pass": pass_no, "case_id": case["case_id"],
            "intent": case["intent"], "category": case["category"],
            "tools": tools, "skills": skills,
            "setup_tools": _names(setup),
            "setup_reply": str(setup.get("response_text") or "")[:200],
            "selected_skill": res.get("selected_skill"),
            "cv_status": cv.get("status"),
            "cv_errors": [
                {"type": e.get("type"), "claim": e.get("claim")}
                for e in (cv.get("errors") or [])
            ],
            "response_text": str(res.get("response_text") or "")[:600],
            "status": res.get("status"),
        })
    except Exception as exc:
        emit({"run": "A", "pass": pass_no, "case_id": case["case_id"],
              "intent": case["intent"], "category": case["category"],
              "error": f"{type(exc).__name__}: {exc}"[:300]})

# ---------------- Run B: ungated all-tools baseline ----------------
from agents import Agent, Runner, FunctionTool  # noqa: E402
from advisor.tools.deterministic_tools.agent_tool_catalog import (  # noqa: E402
    iter_agent_tool_specs,
)

def _mk_tool(spec):
    name = spec["name"]

    async def _invoke(ctx, args_json):
        return json.dumps({"ok": False,
                           "error": "execution stubbed in baseline harness"})

    schema = dict(spec.get("parameters") or
                  {"type": "object", "properties": {},
                   "additionalProperties": False})
    return FunctionTool(
        name=name,
        description=str(spec.get("description") or name)[:900],
        params_json_schema=schema,
        on_invoke_tool=_invoke,
        strict_json_schema=False,
    )

ALL_TOOLS = [_mk_tool(s) for s in iter_agent_tool_specs()]

BASELINE = Agent(
    name="Baseline Advisor",
    instructions=(
        "You are AWM, a wealth-management advisor. You have direct access to "
        "every tool. Use whichever tools help you answer the client."),
    tools=ALL_TOOLS,
    model="deepseek-v4-flash",
)

def run_ungated(case):
    try:
        prompt = ("(My advisor file already holds, confirmed: " + SETUP_MSG
                  + ")\n\n")
        ctx = context_turns(case)
        if ctx:
            prompt += ctx[0]["content"] + "\n\n"
        prompt += case["prompt"]
        async def _go():
            return await Runner.run(BASELINE, prompt, max_turns=8)
        res = asyncio.run(_go())
        tools = [i.raw_item.name for i in res.new_items
                 if type(i).__name__ == "ToolCallItem"]
        emit({"run": "B", "pass": 0, "case_id": case["case_id"],
              "intent": case["intent"], "category": case["category"],
              "tools": tools,
              "response_text": str(res.final_output or "")[:400]})
    except Exception as exc:
        emit({"run": "B", "pass": 0, "case_id": case["case_id"],
              "intent": case["intent"], "category": case["category"],
              "error": f"{type(exc).__name__}: {exc}"[:300],
              "tools": []})

# ---------------- driver ----------------
def main():
    import os
    global DIVERSE
    if os.environ.get("SMOKE"):
        DIVERSE = [DIVERSE[0], DIVERSE[95]]  # one cashflow, one no_quant
    done = done_keys()
    jobs = []
    for c in DIVERSE:
        for p in (1, 2):
            if ("A", p, c["case_id"]) not in done:
                jobs.append(("A", p, c))
        if ("B", 0, c["case_id"]) not in done:
            jobs.append(("B", 0, c))
    print(f"jobs remaining: {len(jobs)} (done: {len(done)})", flush=True)

    def work(job):
        kind, p, c = job
        if kind == "A":
            run_gated(c, p)
        else:
            run_ungated(c)
        return c["case_id"]

    with ThreadPoolExecutor(max_workers=14) as ex:
        for i, cid in enumerate(ex.map(work, jobs), 1):
            if i % 10 == 0:
                print(f"{i}/{len(jobs)} done", flush=True)
    print("ALL DONE", flush=True)

if __name__ == "__main__":
    main()

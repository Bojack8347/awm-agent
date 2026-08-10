# -*- coding: utf-8 -*-
"""P3 latency remeasure: rows 1-2 of tab:latency, median of 20, in-process."""
import json
import os
import platform
import statistics
import sys
import time
import uuid

from pathlib import Path as _P
REPO = str(_P(__file__).resolve().parents[1])
sys.path.insert(0, REPO + r"\backend")

from gate_eval import SETUP_MSG  # noqa: E402
from advisor.runtime.service import build_production_advisor_runtime  # noqa: E402

RUNTIME = build_production_advisor_runtime()
cid = f"lat-{uuid.uuid4().hex[:6]}"
sid = f"companion-{cid}"
res = RUNTIME.run_turn(client_id=cid, session_id=sid, user_message=SETUP_MSG)
if not any(str(t.get("tool")) == "run_cashflow_projection"
           for t in res.get("tool_results") or []):
    res = RUNTIME.run_turn(
        client_id=cid, session_id=sid,
        user_message="Now run the full cashflow projection through the engine.",
        recent_turns=[{"role": "user", "content": SETUP_MSG},
                      {"role": "assistant",
                       "content": str(res.get("response_text") or "")[:600]}],
    )

analysis_id = None
for t in res.get("tool_results") or []:
    name = str(t.get("tool") or t.get("name") or "")
    if name == "run_cashflow_projection":
        blob = json.dumps(t, default=str)
        print("projection result keys:", sorted(t.keys()))
        for k in ("analysis_id",):
            v = t.get(k) or (t.get("result") or {}).get(k) if isinstance(t.get("result"), dict) else t.get(k)
            if v:
                analysis_id = str(v)
        if not analysis_id:
            import re
            m = re.search(r'"analysis_id"\s*:\s*"([^"]+)"', blob)
            analysis_id = m.group(1) if m else None
print("analysis_id:", analysis_id)
if not analysis_id:
    print("NO ANALYSIS ID -- dumping tools:",
          [str(t.get("tool")) for t in res.get("tool_results") or []])
    sys.exit(1)

ex = RUNTIME._tool_executor

# Row 1: exact-year percentile follow-up (stored-analysis lookup, 0 engine calls)
def row1():
    return ex.execute(
        client_id=cid, session_id=sid, tool_name="get_cashflow_analysis",
        arguments={"analysis_id": analysis_id,
                   "calendar_years": [2045],
                   "detail_columns": ["Net Worth"]},
    )

first = row1()
print("row1 first-call ok:", first.get("ok"), "| err:", first.get("error"))
if first.get("ok") is not True:
    print(json.dumps(first, default=str)[:800])
    sys.exit(1)

times1 = []
for _ in range(20):
    t0 = time.perf_counter()
    r = row1()
    times1.append((time.perf_counter() - t0) * 1000)
    assert r.get("ok") is True
print(f"row1 exact-year lookup: median {statistics.median(times1):.2f} ms "
      f"(min {min(times1):.2f}, max {max(times1):.2f}, n=20)")

# Row 2: seeded drawdown distribution, 1,000 paths, in-process
from domain.finance.capital_markets import analyze_allocation_risk_contributions  # noqa: E402

ALLOC = {"US Equity": 0.7, "US Treasury": 0.3}
CFG = {"horizon_years": 30, "num_simulations": 1000, "seed": 42}

warm = analyze_allocation_risk_contributions(ALLOC, drawdown_config=CFG)
print("row2 warm keys:", sorted(warm.keys())[:8])
times2 = []
for _ in range(20):
    t0 = time.perf_counter()
    analyze_allocation_risk_contributions(ALLOC, drawdown_config=CFG)
    times2.append((time.perf_counter() - t0) * 1000)
print(f"row2 drawdown 1000 paths: median {statistics.median(times2):.2f} ms "
      f"(min {min(times2):.2f}, max {max(times2):.2f}, n=20)")

print("hardware:", platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER"),
      "| python", platform.python_version())

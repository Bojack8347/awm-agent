# -*- coding: utf-8 -*-
"""Minimal local conversation loop against the production advisor runtime.

Prerequisites (see README): both engine services running, and your own
LLM credentials exported -- OPENAI_API_KEY (and LLM_BASE_URL for a
non-OpenAI Chat Completions provider such as DeepSeek).
"""
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

if not os.environ.get("OPENAI_API_KEY"):
    sys.exit("Set OPENAI_API_KEY to your own key first (see README).")
os.environ.setdefault("AWM_DATABASE_MODE", "off")

from advisor.runtime.service import build_production_advisor_runtime

runtime = build_production_advisor_runtime()
client_id = f"local-{uuid.uuid4().hex[:6]}"
history = []
print("AWM local advisor. Ctrl-C or empty line to exit.")
while True:
    try:
        msg = input("you> ").strip()
    except (EOFError, KeyboardInterrupt):
        break
    if not msg:
        break
    result = runtime.run_turn(
        client_id=client_id,
        session_id=f"companion-{client_id}",
        user_message=msg,
        recent_turns=history[-12:],
    )
    reply = str(result.get("response_text") or "")
    tools = [str(t.get("tool") or "") for t in result.get("tool_results") or []]
    if tools:
        print(f"[tools: {', '.join(tools)}]")
    print("advisor>", reply)
    history += [{"role": "user", "content": msg},
                {"role": "assistant", "content": reply[:800]}]

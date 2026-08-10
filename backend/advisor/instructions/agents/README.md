# SDK Agent Instructions

These files contain instruction assets for the current OpenAI Agents SDK
workflow.

Every active agent directory contains:

- `agent_contract.txt` for manifest metadata and hard behavioral boundaries.
- `agent_system.txt` for the always-loaded behavioral prompt.

`main_agent/realtime_adapter.txt` is an additional voice-only layer. Skills remain
separate workflow overlays under `instructions/skills/`.

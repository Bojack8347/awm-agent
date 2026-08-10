# Specialist/Sub-Agent Instructions

Callable SDK specialists/sub-agents live here. Each specialist's
`agent_contract.txt` and `agent_system.txt` are loaded by
`backend/advisor/agents/catalog.py`, then combined with any active skill and
runtime context before exposure through the SDK tool wrappers.

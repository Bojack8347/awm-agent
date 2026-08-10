# Task Agent Instructions

Each active task directory contains:

- `task_contract.txt` for validated runtime and LLM configuration.
- `system_prompt.txt` for the task-specific behavioral prompt.

Task runtimes make one bounded model call and do not expose tools or run an
agent loop, so agent-only fields such as `max_turns` and `tool_choice` do not
apply.

Active backend tasks:

- `client_profile`
- `knowledge_updater`
- `cashflow_mapper`
- `activation_mutation`
- `message_composer`
- `conversation_compactor`

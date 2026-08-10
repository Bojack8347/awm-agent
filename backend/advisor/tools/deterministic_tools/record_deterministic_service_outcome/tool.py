"""Agent tool declaration for record_deterministic_service_outcome."""

from __future__ import annotations

from advisor.tools.deterministic_tools._schema import OBJECT_SCHEMA


TOOL_SPEC = {
    "name": "record_deterministic_service_outcome",
    "capability": "deterministic_service_outcome",
    "read_only": False,
    "description": "Record deterministic KYC/consent/execution/settlement/holdings workflow outcomes.",
    "writeback_target": "client_file.services",
}

PARAMS_SCHEMA = OBJECT_SCHEMA

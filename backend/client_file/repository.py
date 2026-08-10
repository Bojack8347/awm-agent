"""Repository boundary for the AWM Client File spine.

The target architecture has one living Client File per client. During migration
we keep existing persistence tables as source systems and expose a single
advisor-facing repository that reads the projected Client File view and writes
sourced events back to the durable event stream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from client_file.interfaces import (
    BusinessEventClientFileWriter,
    ClientFileReader,
    ClientFileSnapshot,
    ClientFileWriter,
    ClientStateViewReader,
)


@dataclass
class ClientFileRepository:
    """Single read/write boundary used by advisor runtime code."""

    reader: ClientFileReader
    writer: ClientFileWriter

    def read(self, client_id: str) -> ClientFileSnapshot:
        return self.reader.read(client_id)

    def write_event(
        self,
        client_id: str,
        *,
        event_type: str,
        payload: Dict[str, Any],
        source: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self.writer.write_event(
            client_id,
            event_type=event_type,
            payload=payload,
            source=source,
        )


def build_production_client_file_repository() -> ClientFileRepository:
    """Build the production Client File boundary.

    Reads are currently served by `services.client_state_view`, a projection
    over existing stores. Writes are durable `client_file.writeback` events.
    This keeps one agent-facing contract while the physical store is migrated.
    """

    return ClientFileRepository(
        reader=ClientStateViewReader(),
        writer=BusinessEventClientFileWriter(),
    )

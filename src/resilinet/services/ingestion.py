"""Inbound ingestion: idempotency and durable-intake seam.

Phase 0 keeps accepted message IDs in memory. Task 4 replaces this with the
relational intake transaction without changing the interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from ..ports import RawMessage


@dataclass(frozen=True, slots=True)
class AcceptResult:
    duplicate: bool
    message_id: UUID


class IngestionService:
    def __init__(self, incident_id: UUID) -> None:
        self._incident_id = incident_id
        self._message_ids: dict[tuple[UUID, str], UUID] = {}
        self._completed: set[UUID] = set()

    async def accept(self, raw: RawMessage) -> AcceptResult:
        key = (self._incident_id, raw.provider_message_id)
        existing = self._message_ids.get(key)
        if existing is not None:
            return AcceptResult(
                duplicate=existing in self._completed,
                message_id=existing,
            )
        message_id = uuid4()
        self._message_ids[key] = message_id
        return AcceptResult(duplicate=False, message_id=message_id)

    def complete(self, message_id: UUID) -> None:
        """Mark a message's domain result as committed.

        A message is only a duplicate once its result is cached and marked
        complete, so a failure before the commit can be retried.
        """
        self._completed.add(message_id)

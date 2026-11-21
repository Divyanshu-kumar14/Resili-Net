"""Append-only audit events.

Payloads identify internal rows and reference codes, never sender addresses,
raw message bodies, tokens, or provider payloads.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import EventLog
from ..observability import current_trace_id, new_trace_id


async def record_event(
    session: AsyncSession,
    *,
    incident_id: UUID,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
    trace_id: str | None = None,
    actor_type: str = "system",
    actor_id: UUID | None = None,
    aggregate_type: str | None = None,
    aggregate_id: UUID | None = None,
) -> EventLog:
    """Append one audit event to the caller's transaction."""
    event = EventLog(
        incident_id=incident_id,
        event_type=event_type,
        trace_id=trace_id or current_trace_id() or new_trace_id(),
        actor_type=actor_type,
        actor_id=actor_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=dict(payload or {}),
    )
    session.add(event)
    await session.flush()
    return event


__all__ = ["record_event"]

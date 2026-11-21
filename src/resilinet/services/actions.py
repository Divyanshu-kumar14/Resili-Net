"""Scoped, single-use action grants.

A grant is a bearer capability for one participant action (for example
confirming one allocation). Only the SHA-256 hash of the token is stored;
the plaintext exists once, inside the private email that carries the link.
Grant *execution* (session + CSRF + authorization checks) is Task 14; this
module owns grant creation so the Task 12 notification handoff can be
transactional with the reservation it refers to.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ActionGrant, PendingActionKind

DEFAULT_GRANT_TTL = timedelta(minutes=30)


def hash_token(token: str) -> str:
    """The stored form of an action token: its SHA-256 hex digest."""
    return hashlib.sha256(token.encode()).hexdigest()


async def create_action_grant(
    session: AsyncSession,
    *,
    kind: PendingActionKind,
    incident_id: UUID,
    sender_id: UUID,
    entity_id: UUID | None = None,
    need_id: UUID | None = None,
    allocation_id: UUID | None = None,
    pending_action_id: UUID | None = None,
    payload_hash: str | None = None,
    now: datetime,
    ttl: timedelta = DEFAULT_GRANT_TTL,
) -> tuple[ActionGrant, str]:
    """Create a grant inside the caller's transaction.

    Returns the row and the plaintext token, which the caller must deliver
    privately (never log it, never put it in a public response).
    """
    token = secrets.token_urlsafe(32)
    grant = ActionGrant(
        id=uuid4(),
        kind=kind,
        incident_id=incident_id,
        sender_id=sender_id,
        entity_id=entity_id,
        need_id=need_id,
        allocation_id=allocation_id,
        pending_action_id=pending_action_id,
        payload_hash=payload_hash,
        token_hash=hash_token(token),
        expires_at=now + ttl,
        consumed_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(grant)
    await session.flush()
    return grant, token

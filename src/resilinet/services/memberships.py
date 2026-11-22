"""Incident-scoped membership seeding.

Email control is still proven only by MojoAuth. Seeding a membership creates
the sender row and the incident role an operator has decided to grant; it does
not mark the sender verified and does not issue any token.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    Entity,
    ParticipantRole,
    ParticipantRoleKind,
    Sender,
)
from ..security import normalize_email
from .events import record_event
from .extraction import normalize_entity_name


class MembershipError(RuntimeError):
    """A membership cannot be seeded as requested."""


async def ensure_membership(
    session: AsyncSession,
    *,
    incident_id: UUID,
    address: str,
    role: ParticipantRoleKind,
    entity_name: str | None = None,
) -> tuple[ParticipantRole, Sender]:
    """Create (or return) one sender and one incident-scoped role."""
    normalized = normalize_email(address)
    if not normalized or "@" not in normalized or len(normalized) > 320:
        raise MembershipError("a valid email address is required")

    entity_id: UUID | None = None
    if role is ParticipantRoleKind.OPERATOR:
        if entity_name is not None:
            raise MembershipError("operator roles are incident-wide")
    else:
        if not entity_name or not entity_name.strip():
            raise MembershipError("this role requires an entity name")
        entity = await session.scalar(
            sa.select(Entity).where(
                Entity.incident_id == incident_id,
                Entity.normalized_name == normalize_entity_name(entity_name),
            )
        )
        if entity is None:
            raise MembershipError(f"entity {entity_name!r} was not found")
        entity_id = entity.id

    sender = await session.scalar(
        sa.select(Sender).where(
            Sender.incident_id == incident_id, Sender.address == normalized
        )
    )
    if sender is None:
        sender = Sender(
            id=uuid4(),
            incident_id=incident_id,
            address=normalized,
            verified=False,
        )
        session.add(sender)
        await session.flush()

    existing = await session.scalar(
        sa.select(ParticipantRole).where(
            ParticipantRole.incident_id == incident_id,
            ParticipantRole.sender_id == sender.id,
            ParticipantRole.role == role,
            (
                ParticipantRole.entity_id.is_(None)
                if entity_id is None
                else ParticipantRole.entity_id == entity_id
            ),
        )
    )
    if existing is not None:
        return existing, sender

    membership = ParticipantRole(
        incident_id=incident_id,
        sender_id=sender.id,
        role=role,
        entity_id=entity_id,
    )
    session.add(membership)
    await session.flush()
    await record_event(
        session,
        incident_id=incident_id,
        event_type="membership.seeded",
        actor_type="system",
        aggregate_type="participant_role",
        aggregate_id=membership.id,
        payload={
            "sender_id": str(sender.id),
            "role": role.value,
            "entity_id": str(entity_id) if entity_id is not None else None,
        },
    )
    return membership, sender


__all__ = ["MembershipError", "ensure_membership"]

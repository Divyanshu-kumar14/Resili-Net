"""Membership seeding never grants verification or a token."""

from __future__ import annotations

from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.db import transaction
from resilinet.models import Entity, EventLog, ParticipantRoleKind, Sender
from resilinet.schemas import EntityType
from resilinet.services.memberships import MembershipError, ensure_membership


async def _entity(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> UUID:
    async with transaction(session_factory) as session:
        entity = Entity(
            incident_id=incident_id,
            name="Depot B",
            normalized_name="depot b",
            entity_type=EntityType.DEPOT,
        )
        session.add(entity)
        await session.flush()
        return entity.id


async def test_operator_membership_is_incident_wide_and_idempotent(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    async with transaction(session_factory) as session:
        first, sender = await ensure_membership(
            session,
            incident_id=incident_id,
            address="Ops@Example.org",
            role=ParticipantRoleKind.OPERATOR,
        )
    async with transaction(session_factory) as session:
        second, again = await ensure_membership(
            session,
            incident_id=incident_id,
            address="ops@example.org",
            role=ParticipantRoleKind.OPERATOR,
        )
    assert first.id == second.id
    assert sender.id == again.id
    assert sender.address == "ops@example.org"
    assert sender.verified is False
    assert second.entity_id is None
    async with session_factory() as session:
        events = list(
            await session.scalars(
                sa.select(EventLog).where(EventLog.event_type == "membership.seeded")
            )
        )
        senders = list(await session.scalars(sa.select(Sender)))
    assert len(events) == 1
    assert len(senders) == 1


async def test_entity_roles_require_an_existing_entity(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    entity_id = await _entity(session_factory, incident_id)
    async with transaction(session_factory) as session:
        role, _sender = await ensure_membership(
            session,
            incident_id=incident_id,
            address="depot@example.org",
            role=ParticipantRoleKind.DEPOT_COORDINATOR,
            entity_name="Depot B",
        )
    assert role.entity_id == entity_id

    with pytest.raises(MembershipError, match="requires an entity name"):
        async with transaction(session_factory) as session:
            await ensure_membership(
                session,
                incident_id=incident_id,
                address="depot2@example.org",
                role=ParticipantRoleKind.DEPOT_COORDINATOR,
            )

    with pytest.raises(MembershipError, match="was not found"):
        async with transaction(session_factory) as session:
            await ensure_membership(
                session,
                incident_id=incident_id,
                address="depot3@example.org",
                role=ParticipantRoleKind.ENTITY_COORDINATOR,
                entity_name="Nowhere",
            )


async def test_operator_role_rejects_an_entity_scope(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    await _entity(session_factory, incident_id)
    with pytest.raises(MembershipError, match="incident-wide"):
        async with transaction(session_factory) as session:
            await ensure_membership(
                session,
                incident_id=incident_id,
                address="ops@example.org",
                role=ParticipantRoleKind.OPERATOR,
                entity_name="Depot B",
            )

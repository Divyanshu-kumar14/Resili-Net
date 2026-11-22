"""Coordinator grant handoff properties (Task 12, review finding F14)."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.adapters.fakes import FakeGeocoder, FakeInboxProvider, FakeIntelligenceProvider
from resilinet.db import transaction
from resilinet.models import (
    ActionGrant,
    OutboundEmail,
    ParticipantRole,
    ParticipantRoleKind,
    PendingActionKind,
    Sender,
)
from resilinet.schemas import (
    FieldConfidence,
    ParsedEmail,
    ParsedReport,
    ReportContext,
    ReportKind,
    Resource,
)
from resilinet.services.extraction import ExtractionService
from resilinet.services.notifications import NotificationService
from resilinet.services.reviews import ReviewService
from resilinet.workers.handlers import MessageJobHandler

from .conftest import NOW, Clock
from .test_reservations import add_entity, make_need, make_supply, service


def _handler(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> MessageJobHandler:
    parsed = ParsedEmail(
        context=ReportContext(
            kind=ReportKind.NEED,
            entity_name="Shelter 4",
            confidence=FieldConfidence(location=0.9, items=0.9, headcount=0.9),
        ),
        reports=[ParsedReport(resource=Resource.POTABLE_WATER)],
    )
    intelligence = FakeIntelligenceProvider({"need": parsed})
    return MessageJobHandler(
        session_factory=session_factory,
        inbox=FakeInboxProvider(webhook_secret="secret", now=clock),
        extraction=ExtractionService(incident_id, intelligence, FakeGeocoder({})),
        notifications=NotificationService(),
        intelligence=intelligence,
        public_base_url="http://testserver",
        clock=clock,
    )


async def test_expired_grant_is_reissued_and_live_grant_is_not_duplicated(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    from resilinet.models import Allocation, EntityType, Need
    from resilinet.services.allocations import AllocationService

    svc: AllocationService = service(clock)
    handler = _handler(session_factory, incident_id, clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "80")
        await make_supply(session, svc, incident_id, depot, "100")
        drafts = await svc.drafts_for_need(session, need.id, NOW)
        result = await svc.commit(
            session, need_id=need.id, drafts=drafts, critic_verdict="approved"
        )
        allocation = result.allocations[0]

        coordinator = Sender(
            incident_id=incident_id,
            address="coordinator@example.org",
            verified=True,
        )
        session.add(coordinator)
        await session.flush()
        session.add(
            ParticipantRole(
                incident_id=incident_id,
                sender_id=coordinator.id,
                role=ParticipantRoleKind.DEPOT_COORDINATOR,
                entity_id=depot.id,
            )
        )
        # An expired, unconsumed grant from an earlier attempt.
        session.add(
            ActionGrant(
                id=uuid4(),
                kind=PendingActionKind.CONFIRM_ALLOCATION,
                incident_id=incident_id,
                sender_id=coordinator.id,
                entity_id=depot.id,
                need_id=need.id,
                allocation_id=allocation.id,
                token_hash="expired-hash",
                expires_at=NOW - timedelta(hours=1),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await session.flush()

        reviews = ReviewService(session)
        need_row = await session.get(Need, need.id)
        allocation_row = await session.get(Allocation, allocation.id)
        assert need_row is not None and allocation_row is not None
        await handler._grant_or_review(
            session, reviews, allocation_row, need_row, NOW, mailbox_id="mb-1"
        )
        await handler._grant_or_review(
            session, reviews, allocation_row, need_row, NOW, mailbox_id="mb-1"
        )

        grants = list(
            await session.scalars(
                sa.select(ActionGrant).order_by(ActionGrant.created_at)
            )
        )
        emails = list(
            await session.scalars(
                sa.select(OutboundEmail).where(
                    OutboundEmail.kind == f"confirm_request:{allocation.ref}"
                )
            )
        )
    # Expired grant reissued exactly once; the live one is never duplicated.
    assert len(grants) == 2
    live = [g for g in grants if g.token_hash != "expired-hash"]
    assert len(live) == 1
    assert live[0].expires_at == NOW + timedelta(minutes=30)
    assert len(emails) == 1
    assert emails[0].to_address == "coordinator@example.org"
    assert "/actions/" in emails[0].body_text


async def test_grant_expiring_during_critic_latency_is_reissued(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    from resilinet.models import Allocation, EntityType, Need
    from resilinet.services.allocations import AllocationService

    svc: AllocationService = service(clock)
    handler = _handler(session_factory, incident_id, clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "80")
        await make_supply(session, svc, incident_id, depot, "100")
        drafts = await svc.drafts_for_need(session, need.id, NOW)
        result = await svc.commit(
            session, need_id=need.id, drafts=drafts, critic_verdict="approved"
        )
        allocation = result.allocations[0]
        coordinator = Sender(
            incident_id=incident_id,
            address="coordinator@example.org",
            verified=True,
        )
        session.add(coordinator)
        await session.flush()
        session.add(
            ParticipantRole(
                incident_id=incident_id,
                sender_id=coordinator.id,
                role=ParticipantRoleKind.DEPOT_COORDINATOR,
                entity_id=depot.id,
            )
        )
        await session.flush()
        await handler._grant_or_review(
            session,
            ReviewService(session),
            await session.get(Allocation, allocation.id),
            await session.get(Need, need.id),
            NOW,
            mailbox_id="mb-1",
        )

    # The critic call took longer than the grant TTL. The next pass still
    # passes the old `now`, but the grant is reissued from the real clock.
    clock.advance(minutes=31)
    async with transaction(session_factory) as session:
        await handler._grant_or_review(
            session,
            ReviewService(session),
            await session.get(Allocation, allocation.id),
            await session.get(Need, need.id),
            NOW,
            mailbox_id="mb-1",
        )

    async with session_factory() as session:
        grants = list(
            await session.scalars(sa.select(ActionGrant).order_by(ActionGrant.created_at))
        )
    assert len(grants) == 2
    live = [
        grant
        for grant in grants
        if grant.consumed_at is None and grant.expires_at > clock.now
    ]
    assert len(live) == 1
    assert live[0].expires_at == clock.now + timedelta(minutes=30)


async def test_coordinator_change_invalidates_the_old_grant_and_issues_a_new_one(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    from resilinet.models import Allocation, EntityType, Need
    from resilinet.services.allocations import AllocationService

    svc: AllocationService = service(clock)
    handler = _handler(session_factory, incident_id, clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "80")
        await make_supply(session, svc, incident_id, depot, "100")
        drafts = await svc.drafts_for_need(session, need.id, NOW)
        result = await svc.commit(
            session, need_id=need.id, drafts=drafts, critic_verdict="approved"
        )
        allocation = result.allocations[0]
        first = Sender(
            incident_id=incident_id,
            address="coordinator-a@example.org",
            verified=True,
        )
        session.add(first)
        await session.flush()
        session.add(
            ParticipantRole(
                incident_id=incident_id,
                sender_id=first.id,
                role=ParticipantRoleKind.DEPOT_COORDINATOR,
                entity_id=depot.id,
            )
        )
        await session.flush()
        await handler._grant_or_review(
            session,
            ReviewService(session),
            await session.get(Allocation, allocation.id),
            await session.get(Need, need.id),
            NOW,
            mailbox_id="mb-1",
        )
        first.verified = False
        second = Sender(
            incident_id=incident_id,
            address="coordinator-b@example.org",
            verified=True,
        )
        session.add(second)
        await session.flush()
        session.add(
            ParticipantRole(
                incident_id=incident_id,
                sender_id=second.id,
                role=ParticipantRoleKind.DEPOT_COORDINATOR,
                entity_id=depot.id,
            )
        )
        await session.flush()
        await handler._grant_or_review(
            session,
            ReviewService(session),
            await session.get(Allocation, allocation.id),
            await session.get(Need, need.id),
            NOW,
            mailbox_id="mb-1",
        )

    async with session_factory() as session:
        grants = list(
            await session.scalars(sa.select(ActionGrant).order_by(ActionGrant.created_at))
        )
        emails = list(
            await session.scalars(
                sa.select(OutboundEmail).where(
                    OutboundEmail.kind.like("confirm_request:%")
                )
            )
        )
    first_grants = [grant for grant in grants if grant.sender_id == first.id]
    second_grants = [grant for grant in grants if grant.sender_id == second.id]
    assert len(first_grants) == 1 and first_grants[0].consumed_at is not None
    assert len(second_grants) == 1 and second_grants[0].consumed_at is None
    assert any(email.to_address == second.address for email in emails)
    assert all("/actions/" in email.body_text for email in emails)

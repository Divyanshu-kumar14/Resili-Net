"""Golden path over the durable pipeline (Phase 2).

Recorded/fake flow only: inbound email → durable intake → strict extraction
→ registration → deterministic drafts → bounded critic → transactional
proposed reservation → threaded recommendation + private coordinator grant
→ privacy-safe map snapshot. No live sponsor is involved or claimed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.adapters.fakes import (
    FakeAuthProvider,
    FakeGeocoder,
    FakeInboxProvider,
    FakeIntelligenceProvider,
)
from resilinet.config import Settings
from resilinet.db import transaction
from resilinet.models import (
    ActionGrant,
    Allocation,
    Entity,
    JobType,
    Message,
    Need,
    ParticipantRole,
    ParticipantRoleKind,
    PendingAction,
    PendingActionKind,
    ReviewCase,
    ReviewCaseKind,
    Sender,
    Supply,
)
from resilinet.ports import InboundEvent, RawMessage
from resilinet.schemas import (
    EntityType,
    FieldConfidence,
    GeoConfidence,
    ParsedEmail,
    ParsedReport,
    ReportContext,
    ReportKind,
    Resource,
    Urgency,
)
from resilinet.security import Principal
from resilinet.services.actions import ActionService, create_action_grant
from resilinet.services.allocations import AllocationService
from resilinet.services.extraction import ExtractionService
from resilinet.services.identity import IdentityService
from resilinet.services.ingestion import IngestionService
from resilinet.services.notifications import NotificationService, OutboxService
from resilinet.services.public import map_snapshot
from resilinet.workers.handlers import MessageJobHandler
from resilinet.workers.runner import DurableWorker

from .conftest import NOW, Clock

PLACES = {"Gandhi Park": (Decimal("22.572600"), Decimal("88.363900"))}
CONFIDENCE = FieldConfidence(location=0.95, items=0.95, headcount=0.95)


def _parsed(
    kind: ReportKind,
    entity: str,
    entity_type: EntityType,
    reports: list[ParsedReport],
    *,
    headcount: int | None = None,
) -> ParsedEmail:
    return ParsedEmail(
        context=ReportContext(
            kind=kind,
            entity_name=entity,
            entity_type=entity_type,
            location_text="near Gandhi Park",
            headcount=headcount,
            urgency=Urgency.HIGH,
            confidence=CONFIDENCE,
        ),
        reports=reports,
    )


FIXTURES: dict[str, ParsedEmail] = {
    "supply-500": _parsed(
        ReportKind.SUPPLY,
        "Depot B",
        EntityType.DEPOT,
        [ParsedReport(resource=Resource.POTABLE_WATER, quantity=Decimal("500"), unit="l")],
    ),
    "supply-50": _parsed(
        ReportKind.SUPPLY,
        "Depot B",
        EntityType.DEPOT,
        [ParsedReport(resource=Resource.POTABLE_WATER, quantity=Decimal("50"), unit="l")],
    ),
    "supply-a": _parsed(
        ReportKind.SUPPLY,
        "Depot A",
        EntityType.DEPOT,
        [ParsedReport(resource=Resource.POTABLE_WATER, quantity=Decimal("60"), unit="l")],
    ),
    "supply-b": _parsed(
        ReportKind.SUPPLY,
        "Depot B",
        EntityType.DEPOT,
        [ParsedReport(resource=Resource.POTABLE_WATER, quantity=Decimal("60"), unit="l")],
    ),
    "need-100": _parsed(
        ReportKind.NEED,
        "Shelter 4",
        EntityType.SHELTER,
        [ParsedReport(resource=Resource.POTABLE_WATER, quantity=Decimal("100"), unit="l")],
        headcount=40,
    ),
    "zero potable water": _parsed(
        ReportKind.NEED,
        "Shelter 4",
        EntityType.SHELTER,
        [ParsedReport(resource=Resource.POTABLE_WATER, quantity=None, unit=None)],
        headcount=40,
    ),
    "need-medical-water": _parsed(
        ReportKind.NEED,
        "Shelter 4",
        EntityType.SHELTER,
        [
            ParsedReport(
                resource=Resource.POTABLE_WATER,
                quantity=Decimal("120"),
                unit="l",
                raw_text="water for insulin patients",
            )
        ],
        headcount=40,
    ),
    "need-medicine": _parsed(
        ReportKind.NEED,
        "Shelter 4",
        EntityType.SHELTER,
        [
            ParsedReport(
                resource=Resource.MEDICINE,
                quantity=Decimal("10"),
                unit="count",
                raw_text="insulin for two patients",
            )
        ],
    ),
}


@dataclass
class Harness:
    inbox: FakeInboxProvider
    mailbox_id: str
    ingestion: IngestionService
    worker: DurableWorker
    outbox: OutboxService
    intelligence: FakeIntelligenceProvider
    session_factory: async_sessionmaker[AsyncSession]
    incident_id: UUID
    clock: Clock
    action_service: ActionService
    auth: FakeAuthProvider | None = None


HARNESS_SETTINGS = Settings(
    app_env="test",
    incident_key="demo-incident",
    public_base_url="http://testserver",
    _env_file=None,
)


async def build_harness(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
    *,
    auth: FakeAuthProvider | None = None,
    fixtures: dict[str, ParsedEmail] | None = None,
) -> Harness:
    inbox = FakeInboxProvider(webhook_secret="secret", now=clock)
    mailbox = await inbox.provision_inbox("demo-incident")
    intelligence = FakeIntelligenceProvider(fixtures or FIXTURES)
    extraction = ExtractionService(incident_id, intelligence, FakeGeocoder(PLACES))
    identity = (
        IdentityService(
            session_factory, auth, HARNESS_SETTINGS, clock=clock
        )
        if auth is not None
        else None
    )
    handler = MessageJobHandler(
        session_factory=session_factory,
        inbox=inbox,
        extraction=extraction,
        notifications=NotificationService(),
        intelligence=intelligence,
        identity=identity,
        public_base_url="http://testserver",
        clock=clock,
    )
    worker = DurableWorker(
        session_factory,
        {JobType.PROCESS_MESSAGE: handler, JobType.FETCH_CONTENT: handler},
        clock=clock,
        jitter=lambda: 0.0,
        worker_id="golden-worker",
    )
    return Harness(
        inbox=inbox,
        mailbox_id=mailbox.id,
        ingestion=IngestionService(incident_id, session_factory, clock=clock),
        worker=worker,
        outbox=OutboxService(session_factory, inbox, clock=clock),
        intelligence=intelligence,
        session_factory=session_factory,
        incident_id=incident_id,
        clock=clock,
        action_service=ActionService(
            AllocationService(clock=clock), NotificationService(), clock=clock
        ),
        auth=auth,
    )


async def accept_message(
    harness: Harness,
    *,
    provider_message_id: str,
    subject: str,
    text: str,
    sender: str,
) -> None:
    """Durable intake only: store the provider message and accept the event."""
    harness.inbox.deliver_inbound(
        mailbox_id=harness.mailbox_id,
        provider_message_id=provider_message_id,
        sender=sender,
        subject=subject,
        text=text,
        thread_id=f"thread-{provider_message_id}",
        received_at=NOW,
    )
    event = InboundEvent(
        event_id=f"evt-{provider_message_id}",
        event_type="message.received",
        mailbox_id=harness.mailbox_id,
        provider_message_id=provider_message_id,
        sender=sender,
        subject=subject,
        received_at=NOW,
        withheld=False,
        mailbox_address="incident@example.org",
        thread_id=f"thread-{provider_message_id}",
        raw=RawMessage(
            provider_message_id=provider_message_id,
            mailbox_id=harness.mailbox_id,
            sender=sender,
            subject=subject,
            text=text,
            received_at=NOW,
            thread_id=f"thread-{provider_message_id}",
        ),
    )
    await harness.ingestion.accept(event)


async def deliver(
    harness: Harness,
    *,
    provider_message_id: str,
    subject: str,
    text: str,
    sender: str,
    auto_apply_supply: bool = True,
) -> None:
    await accept_message(
        harness,
        provider_message_id=provider_message_id,
        subject=subject,
        text=text,
        sender=sender,
    )
    assert await harness.worker.run_once() == 1
    if auto_apply_supply:
        # In production a supply email only produces a private action link.
        # This harness performs that same authorized action as test setup so
        # the matching assertions below see the resulting stock.
        await apply_pending_supply_actions(harness, provider_message_id)
        for _ in range(10):
            if await harness.worker.run_once() == 0:
                break
    while await harness.outbox.deliver_due():
        pass


async def apply_pending_supply_actions(
    harness: Harness, provider_message_id: str
) -> None:
    """Execute this message's UPDATE_SUPPLY pending actions as its sender."""
    async with harness.session_factory() as session:
        message = await session.scalar(
            sa.select(Message).where(
                Message.provider_message_id == provider_message_id
            )
        )
        if message is None:
            return
        pending_ids = list(
            await session.scalars(
                sa.select(PendingAction.id).where(
                    PendingAction.source_message_id == message.id,
                    PendingAction.kind == PendingActionKind.UPDATE_SUPPLY,
                )
            )
        )
    for pending_id in pending_ids:
        async with transaction(harness.session_factory) as session:
            pending = await session.get(
                PendingAction, pending_id, with_for_update=True
            )
            if pending is None or pending.entity_id is None:
                continue
            sender = await session.get(
                Sender, pending.sender_id, with_for_update=True
            )
            if sender is None:  # pragma: no cover - FK guarantees the row
                continue
            if not sender.verified:
                sender.verified = True
                sender.verified_at = harness.clock.now
            role = await session.scalar(
                sa.select(ParticipantRole.id).where(
                    ParticipantRole.incident_id == pending.incident_id,
                    ParticipantRole.sender_id == sender.id,
                    ParticipantRole.role == ParticipantRoleKind.DEPOT_COORDINATOR,
                    ParticipantRole.entity_id == pending.entity_id,
                )
            )
            if role is None:
                session.add(
                    ParticipantRole(
                        incident_id=pending.incident_id,
                        sender_id=sender.id,
                        role=ParticipantRoleKind.DEPOT_COORDINATOR,
                        entity_id=pending.entity_id,
                    )
                )
                await session.flush()
            grant = await session.scalar(
                sa.select(ActionGrant).where(
                    ActionGrant.pending_action_id == pending.id,
                    ActionGrant.consumed_at.is_(None),
                    ActionGrant.expires_at > harness.clock.now,
                )
            )
            if grant is None:
                grant, _token = await create_action_grant(
                    session,
                    kind=PendingActionKind.UPDATE_SUPPLY,
                    incident_id=pending.incident_id,
                    sender_id=sender.id,
                    entity_id=pending.entity_id,
                    pending_action_id=pending.id,
                    payload_hash=pending.payload_hash,
                    now=harness.clock.now,
                )
            principal = Principal(
                session_id=uuid4(),
                incident_id=pending.incident_id,
                sender_id=sender.id,
                address=sender.address,
                expires_at=harness.clock.now + timedelta(hours=1),
                csrf_token="test-csrf",
            )
            await harness.action_service.execute(
                session, grant_id=grant.id, principal=principal
            )


async def seed_supply(
    harness: Harness,
    *,
    entity_name: str = "Depot B",
    resource: Resource = Resource.POTABLE_WATER,
    quantity: Decimal = Decimal("500"),
    unit: str = "l",
) -> None:
    """Seed stock directly for tests that are not about the email action."""
    async with transaction(harness.session_factory) as session:
        entity = await session.scalar(
            sa.select(Entity).where(
                Entity.incident_id == harness.incident_id,
                Entity.normalized_name == entity_name.casefold(),
            )
        )
        if entity is None:
            entity = Entity(
                incident_id=harness.incident_id,
                name=entity_name,
                normalized_name=entity_name.casefold(),
                entity_type=EntityType.DEPOT,
                location_text="near Gandhi Park",
                lat=Decimal("22.572600"),
                lon=Decimal("88.363900"),
                geo_confidence=GeoConfidence.EXACT,
            )
            session.add(entity)
            await session.flush()
        await AllocationService(clock=harness.clock).apply_supply_snapshot(
            session,
            incident_id=harness.incident_id,
            entity_id=entity.id,
            resource=resource,
            quantity=quantity,
            unit=unit,
            source_report_id=None,
        )


async def seed_coordinator(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    *,
    address: str = "coordinator@example.org",
) -> Sender:
    async with transaction(session_factory) as session:
        depot = await session.scalar(
            sa.select(Entity).where(
                Entity.incident_id == incident_id, Entity.name == "Depot B"
            )
        )
        assert depot is not None
        coordinator = Sender(incident_id=incident_id, address=address, verified=True)
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
        return coordinator


def sent_to(harness: Harness, address: str) -> list[str]:
    return [
        item.draft.text for item in harness.inbox.sent_messages if item.draft.to == address
    ]


async def test_need_email_to_recommendation_reply_and_map(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    harness = await build_harness(session_factory, incident_id, clock)
    await deliver(
        harness,
        provider_message_id="msg-supply",
        subject="Depot B stock",
        text="supply-500 Depot B has 500 l of potable water near Gandhi Park.",
        sender="depot@example.org",
    )
    coordinator = await seed_coordinator(session_factory, incident_id)

    await deliver(
        harness,
        provider_message_id="msg-need",
        subject="Shelter 4 water",
        text="Shelter 4 has 40 people and zero potable water near Gandhi Park.",
        sender="volunteer@example.org",
    )

    async with session_factory() as session:
        need = await session.scalar(sa.select(Need))
        allocation = await session.scalar(sa.select(Allocation))
        supply = await session.scalar(sa.select(Supply))
        entities = list(await session.scalars(sa.select(Entity)))
        assert need is not None and allocation is not None and supply is not None

        # Estimated 120 l need, fully reserved as a proposal (not a dispatch).
        assert need.quantity == Decimal("120.000")
        assert need.quantity_source.value == "estimated"
        assert need.status.value == "matched"
        assert allocation.quantity == Decimal("120.000")
        assert allocation.status.value == "proposed"
        assert allocation.critic_verdict == "approved"

        snapshot = map_snapshot(
            incident_id=incident_id,
            entities=entities,
            needs=[need],
            supplies=[supply],
            allocations=[allocation],
            available_by_supply={
                supply.id: await AllocationService(clock=clock).available(session, supply)
            },
            generated_at=NOW,
        )

    recommendations = sent_to(harness, "volunteer@example.org")
    assert len(recommendations) == 1
    body = recommendations[0]
    assert need.ref in body and allocation.ref in body
    assert "~120 l" in body and "estimated from 40 people" in body
    assert "Recommended:" in body
    assert "not a shipment or a dispatch" in body
    assert "/actions/" not in body  # requesters never receive grant links

    confirmations = sent_to(harness, coordinator.address)
    assert len(confirmations) == 1
    assert allocation.ref in confirmations[0]
    token_match = re.search(r"/actions/([A-Za-z0-9_-]+)", confirmations[0])
    assert token_match is not None
    token = token_match.group(1)

    async with session_factory() as session:
        grant = await session.scalar(
            sa.select(ActionGrant).where(
                ActionGrant.kind == PendingActionKind.CONFIRM_ALLOCATION
            )
        )
    assert grant is not None
    assert grant.allocation_id == allocation.id
    assert grant.sender_id == coordinator.id
    assert grant.kind.value == "CONFIRM_ALLOCATION"
    assert grant.consumed_at is None
    # Only the hash is stored, and the grant lives for 30 minutes.
    import hashlib

    assert grant.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert grant.token_hash != token
    assert (grant.expires_at - NOW).total_seconds() == 30 * 60

    payload = json.dumps(snapshot)
    assert {row["name"] for row in snapshot["entities"]} == {"Shelter 4", "Depot B"}
    assert snapshot["needs"][0]["ref"] == need.ref
    assert snapshot["needs"][0]["quantity_source"] == "estimated"
    assert snapshot["allocations"][0]["ref"] == allocation.ref
    assert snapshot["supplies"][0]["available"] == "380.000"
    # Privacy: no addresses, no tokens, no raw text in the public snapshot.
    assert token not in payload
    assert "@example.org" not in payload
    assert "potable water near Gandhi Park" not in payload


async def test_split_reservation_reports_each_depot_truthfully(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    harness = await build_harness(session_factory, incident_id, clock)
    await deliver(
        harness,
        provider_message_id="msg-supply-a",
        subject="Depot A stock",
        text="supply-a Depot A has 60 l of potable water near Gandhi Park.",
        sender="depot-a@example.org",
    )
    await deliver(
        harness,
        provider_message_id="msg-supply-b",
        subject="Depot B stock",
        text="supply-b Depot B has 60 l of potable water near Gandhi Park.",
        sender="depot-b@example.org",
    )
    await deliver(
        harness,
        provider_message_id="msg-need-100",
        subject="Shelter 4 water",
        text="need-100 Shelter 4 needs 100 l of potable water near Gandhi Park.",
        sender="volunteer@example.org",
    )

    async with session_factory() as session:
        allocations = list(await session.scalars(sa.select(Allocation)))
    assert len(allocations) == 2
    assert {a.quantity for a in allocations} == {Decimal("60.000"), Decimal("40.000")}

    recommendations = sent_to(harness, "volunteer@example.org")
    assert len(recommendations) == 1
    body = recommendations[0]
    # One line per allocation: each depot is credited only with its own part.
    assert body.count("Recommended:") == 2
    assert "Depot A is recommended to supply" in body
    assert "Depot B is recommended to supply" in body
    assert "supply 100 l" not in body
    for allocation in allocations:
        assert allocation.ref in body


async def test_missing_coordinator_creates_a_role_assignment_review(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    harness = await build_harness(session_factory, incident_id, clock)
    # Stock exists, but no verified depot coordinator is assigned, so the
    # proposal cannot be handed to anyone.
    await seed_supply(harness)
    await deliver(
        harness,
        provider_message_id="msg-need",
        subject="Shelter 4 water",
        text="Shelter 4 has 40 people and zero potable water near Gandhi Park.",
        sender="volunteer@example.org",
    )

    async with session_factory() as session:
        allocation = await session.scalar(sa.select(Allocation))
        case = await session.scalar(
            sa.select(ReviewCase).where(
                ReviewCase.kind == ReviewCaseKind.ROLE_ASSIGNMENT
            )
        )
    # The proposal still reserves stock, but no grant was exposed anywhere.
    assert allocation is not None and allocation.status.value == "proposed"
    assert case is not None
    assert all(
        "/actions/" not in item.draft.text for item in harness.inbox.sent_messages
    )


async def test_second_identical_need_corroborates_without_a_second_reservation(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    harness = await build_harness(session_factory, incident_id, clock)
    await deliver(
        harness,
        provider_message_id="msg-supply",
        subject="Depot B stock",
        text="supply-500 Depot B has 500 l of potable water near Gandhi Park.",
        sender="depot@example.org",
    )
    await deliver(
        harness,
        provider_message_id="msg-need-1",
        subject="Shelter 4 water",
        text="Shelter 4 has 40 people and zero potable water near Gandhi Park.",
        sender="volunteer@example.org",
    )
    await deliver(
        harness,
        provider_message_id="msg-need-2",
        subject="Shelter 4 water again",
        text="Shelter 4 has 40 people and zero potable water near Gandhi Park.",
        sender="neighbour@example.org",
    )

    async with session_factory() as session:
        needs = list(await session.scalars(sa.select(Need)))
        allocations = list(await session.scalars(sa.select(Allocation)))
    assert len(needs) == 1
    assert sum(a.quantity for a in allocations) == Decimal("120.000")
    corroboration = sent_to(harness, "neighbour@example.org")
    assert len(corroboration) == 1
    assert "corroboration" in corroboration[0]
    assert needs[0].ref in corroboration[0]


async def test_medical_corroboration_escalates_without_a_new_reservation(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    harness = await build_harness(session_factory, incident_id, clock)
    await deliver(
        harness,
        provider_message_id="msg-supply",
        subject="Depot B stock",
        text="supply-500 Depot B has 500 l of potable water near Gandhi Park.",
        sender="depot@example.org",
    )
    await deliver(
        harness,
        provider_message_id="msg-need-1",
        subject="Shelter 4 water",
        text="Shelter 4 has 40 people and zero potable water near Gandhi Park.",
        sender="volunteer@example.org",
    )
    await deliver(
        harness,
        provider_message_id="msg-need-2",
        subject="Shelter 4 water for patients",
        text="need-medical-water Shelter 4 needs water near Gandhi Park.",
        sender="neighbour@example.org",
    )

    async with session_factory() as session:
        needs = list(await session.scalars(sa.select(Need)))
        allocations = list(await session.scalars(sa.select(Allocation)))
        case = await session.scalar(
            sa.select(ReviewCase).where(ReviewCase.kind == ReviewCaseKind.MEDICAL)
        )
    assert len(needs) == 1
    assert sum(a.quantity for a in allocations) == Decimal("120.000")
    assert case is not None and case.need_id == needs[0].id
    replies = sent_to(harness, "neighbour@example.org")
    assert len(replies) == 1
    assert "must review" in replies[0]
    assert "No stock has been reserved" not in replies[0]
    assert "Existing reservations are unchanged" in replies[0]
    assert "Recommended:" not in replies[0]


async def test_medical_need_is_reviewed_without_a_stock_promise(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    harness = await build_harness(session_factory, incident_id, clock)
    await deliver(
        harness,
        provider_message_id="msg-supply",
        subject="Depot B stock",
        text="supply-500 Depot B has 500 l of potable water near Gandhi Park.",
        sender="depot@example.org",
    )
    await deliver(
        harness,
        provider_message_id="msg-medical",
        subject="Shelter 4 medicine",
        text="need-medicine Shelter 4 needs insulin near Gandhi Park.",
        sender="volunteer@example.org",
    )

    async with session_factory() as session:
        need = await session.scalar(sa.select(Need))
        case = await session.scalar(
            sa.select(ReviewCase).where(ReviewCase.kind == ReviewCaseKind.MEDICAL)
        )
        allocations = list(await session.scalars(sa.select(Allocation)))
    assert need is not None and need.needs_human_review is True
    assert case is not None and case.need_id == need.id
    assert allocations == []
    replies = sent_to(harness, "volunteer@example.org")
    assert len(replies) == 1
    assert "must review" in replies[0]
    assert "not a supply promise" in replies[0]
    assert "Recommended:" not in replies[0]


async def test_later_supply_rematches_an_open_need_and_notifies_the_requester(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    harness = await build_harness(session_factory, incident_id, clock)
    await deliver(
        harness,
        provider_message_id="msg-need",
        subject="Shelter 4 water",
        text="Shelter 4 has 40 people and zero potable water near Gandhi Park.",
        sender="volunteer@example.org",
    )
    first = sent_to(harness, "volunteer@example.org")
    assert len(first) == 1 and "No stock is currently reserved" in first[0]

    await deliver(
        harness,
        provider_message_id="msg-supply",
        subject="Depot B stock",
        text="supply-500 Depot B has 500 l of potable water near Gandhi Park.",
        sender="depot@example.org",
    )

    async with session_factory() as session:
        allocation = await session.scalar(sa.select(Allocation))
    assert allocation is not None and allocation.quantity == Decimal("120.000")
    updates = sent_to(harness, "volunteer@example.org")
    assert len(updates) == 2
    assert "Recommended:" in updates[1]


async def test_crash_between_registration_and_matching_recovers_without_duplicates(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    harness = await build_harness(session_factory, incident_id, clock)
    await deliver(
        harness,
        provider_message_id="msg-supply",
        subject="Depot B stock",
        text="supply-500 Depot B has 500 l of potable water near Gandhi Park.",
        sender="depot@example.org",
    )
    coordinator = await seed_coordinator(session_factory, incident_id)

    # The worker dies after the need is registered but before matching commits.
    harness.intelligence.fail_next_critique = RuntimeError("simulated crash")
    await accept_message(
        harness,
        provider_message_id="msg-need",
        subject="Shelter 4 water",
        text="Shelter 4 has 40 people and zero potable water near Gandhi Park.",
        sender="volunteer@example.org",
    )
    await harness.worker.run_once()
    async with session_factory() as session:
        needs = list(await session.scalars(sa.select(Need)))
        allocations = list(await session.scalars(sa.select(Allocation)))
    assert len(needs) == 1 and allocations == []

    clock.advance(seconds=120)
    assert await harness.worker.run_once() == 1
    while await harness.outbox.deliver_due():
        pass

    async with session_factory() as session:
        allocations = list(await session.scalars(sa.select(Allocation)))
    assert len(allocations) == 1
    assert allocations[0].quantity == Decimal("120.000")
    # Exactly one logical recommendation and one private confirm request.
    assert len(sent_to(harness, "volunteer@example.org")) == 1
    assert len(sent_to(harness, coordinator.address)) == 1


async def test_lowered_snapshot_cancels_the_reservation_and_notifies_the_requester(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    harness = await build_harness(session_factory, incident_id, clock)
    await deliver(
        harness,
        provider_message_id="msg-supply",
        subject="Depot B stock",
        text="supply-500 Depot B has 500 l of potable water near Gandhi Park.",
        sender="depot@example.org",
    )
    await deliver(
        harness,
        provider_message_id="msg-need",
        subject="Shelter 4 water",
        text="Shelter 4 has 40 people and zero potable water near Gandhi Park.",
        sender="volunteer@example.org",
    )
    await deliver(
        harness,
        provider_message_id="msg-supply-2",
        subject="Depot B correction",
        text="supply-50 Depot B now has only 50 l of potable water near Gandhi Park.",
        sender="depot@example.org",
    )

    async with session_factory() as session:
        need = await session.scalar(sa.select(Need))
        allocations = list(await session.scalars(sa.select(Allocation)))
        supply = await session.scalar(sa.select(Supply))
    assert supply is not None and supply.on_hand == Decimal("50.000")
    by_status = {a.status.value: a for a in allocations}
    # The unsupported 120 l reservation is cancelled; the remaining 50 l of
    # real stock immediately fills the need partially, by priority.
    assert by_status["cancelled"].quantity == Decimal("120.000")
    assert by_status["proposed"].quantity == Decimal("50.000")
    assert need is not None and need.status.value == "partial"
    notices = sent_to(harness, "volunteer@example.org")
    assert any(
        "was cancelled because the available stock changed" in body
        for body in notices
    )

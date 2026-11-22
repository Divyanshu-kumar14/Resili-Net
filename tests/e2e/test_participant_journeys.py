"""Task 15: participant journeys over the durable worker and action pages.

These are end-to-end Fake flows: an email is parsed, stored as an immutable
PendingAction, only a verified session plus CSRF POST applies it, and the
worker performs the residual rematch. No live sponsor is involved.
"""

from __future__ import annotations

import re
from decimal import Decimal
from uuid import UUID

import httpx
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.adapters.fakes import FakeAuthProvider
from resilinet.db import transaction
from resilinet.domain.quantities import QuantitySource
from resilinet.main import Container, create_app
from resilinet.models import (
    ActionGrant,
    Allocation,
    Entity,
    Incident,
    Job,
    JobStatus,
    JobType,
    Message,
    Need,
    OutboundEmail,
    ParticipantRole,
    ParticipantRoleKind,
    PendingAction,
    PendingActionKind,
    PendingActionStatus,
    ReviewCase,
    ReviewCaseKind,
    Sender,
    Supply,
    VerificationRequest,
    VerificationStatus,
)
from resilinet.schemas import (
    AllocationStatus,
    EntityType,
    FieldConfidence,
    NeedStatus,
    ParsedEmail,
    ParsedReport,
    ReportContext,
    ReportKind,
    Resource,
)
from resilinet.security import (
    AUTH_NONCE_COOKIE,
    SESSION_COOKIE_NAME,
    derive_csrf_token,
    hash_token,
)
from resilinet.services.allocations import AllocationService

from .conftest import NOW, Clock
from .test_golden_path import (
    FIXTURES as BASE_FIXTURES,
)
from .test_golden_path import (
    HARNESS_SETTINGS,
    Harness,
    build_harness,
    deliver,
    sent_to,
)

CONFIDENCE = FieldConfidence(location=0.95, items=0.95, headcount=0.95)


def _parsed(
    kind: ReportKind,
    entity: str,
    entity_type: EntityType,
    reports: list[ParsedReport],
    *,
    reference_id: str | None = None,
    headcount: int | None = None,
) -> ParsedEmail:
    return ParsedEmail(
        context=ReportContext(
            kind=kind,
            entity_name=entity,
            entity_type=entity_type,
            location_text="near Gandhi Park",
            reference_id=reference_id,
            headcount=headcount,
            confidence=CONFIDENCE,
        ),
        reports=reports,
    )


def make_app(harness: Harness, clock: Clock) -> FastAPI:
    container = Container(
        settings=HARNESS_SETTINGS,
        session_factory=harness.session_factory,
        inbox=harness.inbox,
        auth=harness.auth,
        clock=clock,
    )
    return create_app(HARNESS_SETTINGS, container)


async def pending_state_for(
    session_factory: async_sessionmaker[AsyncSession], address: str
) -> str | None:
    async with session_factory() as session:
        return await session.scalar(
            sa.select(VerificationRequest.provider_state_id)
            .join(Sender, Sender.id == VerificationRequest.sender_id)
            .where(
                Sender.address == address,
                VerificationRequest.status == VerificationStatus.PENDING,
            )
            .order_by(VerificationRequest.created_at.desc())
            .limit(1)
        )


async def login(
    client: httpx.AsyncClient,
    auth: FakeAuthProvider,
    session_factory: async_sessionmaker[AsyncSession],
    address: str,
) -> str:
    response = await client.post("/operator/login", data={"email": address})
    assert response.status_code == 200
    state = await pending_state_for(session_factory, address)
    assert state is not None
    page = await client.get("/auth/callback", params={"state_id": state})
    assert page.status_code == 200
    nonce = client.cookies.get(AUTH_NONCE_COOKIE)
    assert nonce is not None
    callback = await client.post(
        "/auth/callback",
        data={"state_id": state, "nonce": nonce},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    token = client.cookies.get(SESSION_COOKIE_NAME)
    assert token is not None
    return token


def csrf_for(session_token: str) -> str:
    return derive_csrf_token(hash_token(session_token))


async def token_for_pending(
    session_factory: async_sessionmaker[AsyncSession], pending_id: UUID
) -> str:
    async with session_factory() as session:
        email = await session.scalar(
            sa.select(OutboundEmail).where(
                OutboundEmail.kind == f"action_request:{pending_id}"
            )
        )
    assert email is not None
    match = re.search(r"/actions/([A-Za-z0-9_-]+)", email.body_text)
    assert match is not None
    return match.group(1)


async def pending_of_kind(
    session_factory: async_sessionmaker[AsyncSession],
    kind: PendingActionKind,
    *,
    status: PendingActionStatus = PendingActionStatus.PENDING,
) -> PendingAction:
    async with session_factory() as session:
        row = await session.scalar(
            sa.select(PendingAction)
            .where(PendingAction.kind == kind, PendingAction.status == status)
            .order_by(PendingAction.created_at.desc())
            .limit(1)
        )
        assert row is not None
        return row


async def create_depot_coordinator(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    *,
    address: str,
    entity_name: str,
) -> tuple[Sender, Entity]:
    async with transaction(session_factory) as session:
        entity = await session.scalar(
            sa.select(Entity).where(
                Entity.incident_id == incident_id,
                Entity.normalized_name == entity_name.casefold(),
            )
        )
        if entity is None:
            entity = Entity(
                incident_id=incident_id,
                name=entity_name,
                normalized_name=entity_name.casefold(),
                entity_type=EntityType.DEPOT,
                location_text="near Gandhi Park",
                lat=Decimal("22.572600"),
                lon=Decimal("88.363900"),
            )
            session.add(entity)
            await session.flush()
        sender = Sender(
            incident_id=incident_id,
            address=address,
            verified=True,
            verified_at=NOW,
        )
        session.add(sender)
        await session.flush()
        session.add(
            ParticipantRole(
                incident_id=incident_id,
                sender_id=sender.id,
                role=ParticipantRoleKind.DEPOT_COORDINATOR,
                entity_id=entity.id,
            )
        )
        await session.flush()
        return sender, entity


async def run_jobs(harness: Harness, limit: int = 10) -> None:
    for _ in range(limit):
        if await harness.worker.run_once() == 0:
            break
    while await harness.outbox.deliver_due():
        pass


async def test_supply_email_requires_a_verified_action_and_rematches_residual_need(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    fixtures = {
        **BASE_FIXTURES,
        "supply-300gal": _parsed(
            ReportKind.SUPPLY,
            "Depot B",
            EntityType.DEPOT,
            [
                ParsedReport(
                    resource=Resource.POTABLE_WATER,
                    quantity=Decimal("300"),
                    unit="gallons",
                )
            ],
        ),
    }
    harness = await build_harness(
        session_factory, incident_id, clock, auth=auth, fixtures=fixtures
    )
    # A residual need exists before the stock arrives.
    await deliver(
        harness,
        provider_message_id="msg-need",
        subject="Shelter 4 water",
        text="Shelter 4 has 40 people and zero potable water near Gandhi Park.",
        sender="volunteer@example.org",
    )
    await create_depot_coordinator(
        session_factory, incident_id, address="depot@example.org", entity_name="Depot B"
    )

    await deliver(
        harness,
        provider_message_id="msg-supply",
        subject="Depot B stock",
        text="supply-300gal Depot B has 300 gallons of potable water near Gandhi Park.",
        sender="depot@example.org",
        auto_apply_supply=False,
    )

    async with session_factory() as session:
        supply = await session.scalar(sa.select(Supply))
        pending = await session.scalar(
            sa.select(PendingAction).where(
                PendingAction.kind == PendingActionKind.UPDATE_SUPPLY
            )
        )
        grant = await session.scalar(
            sa.select(ActionGrant).where(
                ActionGrant.kind == PendingActionKind.UPDATE_SUPPLY
            )
        )
    # The email changed nothing; it produced one immutable action.
    assert supply is None
    assert pending is not None and pending.status is PendingActionStatus.PENDING
    assert grant is not None and grant.consumed_at is None
    assert pending.id == grant.pending_action_id

    token = await token_for_pending(session_factory, pending.id)
    app = make_app(harness, clock)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_token = await login(
            client, auth, session_factory, "depot@example.org"
        )
        posted = await client.post(
            f"/actions/{token}", data={"csrf_token": csrf_for(session_token)}
        )
        assert posted.status_code == 200
        assert "Stock snapshot applied" in posted.text

    async with session_factory() as session:
        supply = await session.scalar(sa.select(Supply))
        pending_row = await session.get(PendingAction, pending.id)
    assert supply is not None
    assert supply.on_hand == Decimal("1135.500")
    assert supply.unit == "l"
    assert pending_row is not None
    assert pending_row.status is PendingActionStatus.CONSUMED

    # Residual matching runs as a durable job and notifies the requester.
    await run_jobs(harness)
    async with session_factory() as session:
        allocation = await session.scalar(sa.select(Allocation))
        need = await session.scalar(sa.select(Need))
    assert allocation is not None and allocation.quantity == Decimal("120.000")
    assert need is not None and need.status is NeedStatus.MATCHED
    updates = sent_to(harness, "volunteer@example.org")
    assert any("Recommended:" in body for body in updates)


async def test_requester_partially_then_fully_delivers_own_allocation(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    harness = await build_harness(
        session_factory, incident_id, clock, auth=auth, fixtures=BASE_FIXTURES
    )
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
    async with transaction(session_factory) as session:
        allocation = await session.scalar(sa.select(Allocation))
        need = await session.scalar(sa.select(Need))
        assert allocation is not None and need is not None
        await AllocationService(clock=clock).confirm_allocation(
            session, allocation_id=allocation.id
        )
        allocation_id = allocation.id
        allocation_ref = allocation.ref

    app = make_app(harness, clock)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_token = await login(
            client, auth, session_factory, "volunteer@example.org"
        )
        csrf = csrf_for(session_token)

        for marker, amount in (("deliver-30", Decimal("30")), ("deliver-90", Decimal("90"))):
            harness.intelligence.set_fixture(
                marker,
                _parsed(
                    ReportKind.STATUS_UPDATE,
                    "Shelter 4",
                    EntityType.SHELTER,
                    [
                        ParsedReport(
                            resource=Resource.POTABLE_WATER,
                            quantity=amount,
                            unit="l",
                        )
                    ],
                    reference_id=allocation_ref,
                ),
            )
            await deliver(
                harness,
                provider_message_id=f"msg-{marker}",
                subject=f"Delivery {marker}",
                text=f"{marker} I delivered {amount} l for {allocation_ref} near Gandhi Park.",
                sender="volunteer@example.org",
                auto_apply_supply=False,
            )
            pending = await pending_of_kind(
                session_factory, PendingActionKind.DELIVER_ALLOCATION
            )
            token = await token_for_pending(session_factory, pending.id)
            posted = await client.post(
                f"/actions/{token}", data={"csrf_token": csrf}
            )
            assert posted.status_code == 200

    async with session_factory() as session:
        allocation = await session.get(Allocation, allocation_id)
        supply = await session.scalar(sa.select(Supply))
        grant_count = await session.scalar(
            sa.select(sa.func.count(ActionGrant.id)).where(
                ActionGrant.kind == PendingActionKind.DELIVER_ALLOCATION
            )
        )
    assert allocation is not None
    assert allocation.delivered_quantity == Decimal("120.000")
    assert allocation.status is AllocationStatus.DELIVERED
    assert supply is not None and supply.on_hand == Decimal("380.000")
    assert grant_count == 2


async def test_cancel_need_releases_every_active_sibling(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    harness = await build_harness(
        session_factory, incident_id, clock, auth=auth, fixtures=BASE_FIXTURES
    )
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
        provider_message_id="msg-need",
        subject="Shelter 4 water",
        text="need-100 Shelter 4 needs 100 l of potable water near Gandhi Park.",
        sender="volunteer@example.org",
    )
    async with transaction(session_factory) as session:
        need = await session.scalar(sa.select(Need))
        allocations = list(await session.scalars(sa.select(Allocation)))
        assert need is not None and len(allocations) == 2
        svc = AllocationService(clock=clock)
        for allocation in allocations:
            await svc.confirm_allocation(session, allocation_id=allocation.id)
        need_id = need.id
        need_ref = need.ref
        allocation_ids = [allocation.id for allocation in allocations]

    harness.intelligence.set_fixture(
        "cancel-need",
        _parsed(
            ReportKind.CANCEL,
            "Shelter 4",
            EntityType.SHELTER,
            [ParsedReport(resource=Resource.POTABLE_WATER)],
            reference_id=need_ref,
        ),
    )
    await deliver(
        harness,
        provider_message_id="msg-cancel",
        subject="Cancel need",
        text=f"cancel-need Please cancel {need_ref} near Gandhi Park.",
        sender="volunteer@example.org",
        auto_apply_supply=False,
    )
    pending = await pending_of_kind(session_factory, PendingActionKind.CANCEL_NEED)
    token = await token_for_pending(session_factory, pending.id)

    app = make_app(harness, clock)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_token = await login(
            client, auth, session_factory, "volunteer@example.org"
        )
        posted = await client.post(
            f"/actions/{token}", data={"csrf_token": csrf_for(session_token)}
        )
        assert posted.status_code == 200
        assert "Need cancelled" in posted.text

    async with session_factory() as session:
        need = await session.get(Need, need_id)
        allocations = [
            await session.get(Allocation, allocation_id)
            for allocation_id in allocation_ids
        ]
        supplies = list(await session.scalars(sa.select(Supply)))
    assert need is not None and need.status is NeedStatus.CANCELLED
    assert all(
        allocation is not None and allocation.status is AllocationStatus.CANCELLED
        for allocation in allocations
    )
    svc = AllocationService(clock=clock)
    async with session_factory() as session:
        for supply in supplies:
            assert await svc.available(session, supply) == supply.on_hand


async def test_unverified_depot_update_is_reviewed_without_a_token(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    fixtures = {
        **BASE_FIXTURES,
        "supply-x": _parsed(
            ReportKind.SUPPLY,
            "Depot X",
            EntityType.DEPOT,
            [
                ParsedReport(
                    resource=Resource.POTABLE_WATER,
                    quantity=Decimal("500"),
                    unit="l",
                )
            ],
        ),
    }
    harness = await build_harness(
        session_factory, incident_id, clock, auth=auth, fixtures=fixtures
    )
    await deliver(
        harness,
        provider_message_id="msg-supply-x",
        subject="Depot X stock",
        text="supply-x Depot X has 500 l of potable water near Gandhi Park.",
        sender="stranger@example.org",
        auto_apply_supply=False,
    )

    async with session_factory() as session:
        supply = await session.scalar(sa.select(Supply))
        pending = await session.scalar(sa.select(PendingAction))
        case = await session.scalar(sa.select(ReviewCase))
        grant = await session.scalar(sa.select(ActionGrant))
        verification = await session.scalar(sa.select(VerificationRequest))
    assert supply is None
    assert pending is not None and pending.status is PendingActionStatus.PENDING
    assert case is not None and case.kind is ReviewCaseKind.ROLE_ASSIGNMENT
    assert grant is None
    assert verification is not None
    assert verification.status is VerificationStatus.PENDING
    stranger_mail = sent_to(harness, "stranger@example.org")
    assert stranger_mail
    assert all("/actions/" not in body for body in stranger_mail)
    assert any("depot authority" in body for body in stranger_mail)
    assert any(item.email == "stranger@example.org" for item in auth.sent)


async def test_wrong_sender_cancel_is_denied_without_a_grant(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    harness = await build_harness(
        session_factory, incident_id, clock, auth=auth, fixtures=BASE_FIXTURES
    )
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
    async with session_factory() as session:
        need = await session.scalar(sa.select(Need))
        assert need is not None
        need_ref = need.ref
        need_id = need.id

    harness.intelligence.set_fixture(
        "cancel-attack",
        _parsed(
            ReportKind.CANCEL,
            "Shelter 4",
            EntityType.SHELTER,
            [ParsedReport(resource=Resource.POTABLE_WATER)],
            reference_id=need_ref,
        ),
    )
    await deliver(
        harness,
        provider_message_id="msg-attack",
        subject="Cancel that need",
        text=f"cancel-attack Cancel {need_ref} now.",
        sender="attacker@example.org",
        auto_apply_supply=False,
    )

    async with session_factory() as session:
        need = await session.get(Need, need_id)
        pending = await session.scalar(
            sa.select(PendingAction).where(
                PendingAction.kind == PendingActionKind.CANCEL_NEED
            )
        )
        case = await session.scalar(
            sa.select(ReviewCase).where(ReviewCase.kind == ReviewCaseKind.OTHER)
        )
        grants = list(
            await session.scalars(
                sa.select(ActionGrant).where(
                    ActionGrant.kind == PendingActionKind.CANCEL_NEED
                )
            )
        )
    assert need is not None and need.status is NeedStatus.MATCHED
    assert pending is not None and pending.status is PendingActionStatus.PENDING
    assert case is not None and case.kind is ReviewCaseKind.OTHER
    assert grants == []
    replies = sent_to(harness, "attacker@example.org")
    assert replies
    assert all("/actions/" not in body for body in replies)


async def test_query_status_is_read_only_and_scoped_to_authorized_sender(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    harness = await build_harness(
        session_factory, incident_id, clock, auth=auth, fixtures=BASE_FIXTURES
    )
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
    async with session_factory() as session:
        need = await session.scalar(sa.select(Need))
        allocation = await session.scalar(sa.select(Allocation))
        assert need is not None and allocation is not None
        need_ref = need.ref
        allocation_ref = allocation.ref
        before = (need.status, allocation.status)

    harness.intelligence.set_fixture(
        "query-status",
        _parsed(
            ReportKind.QUERY,
            "Shelter 4",
            EntityType.SHELTER,
            [ParsedReport(resource=Resource.POTABLE_WATER)],
            reference_id=need_ref,
        ),
    )
    await deliver(
        harness,
        provider_message_id="msg-query",
        subject="Status",
        text=f"query-status QUERY STATUS {need_ref}",
        sender="volunteer@example.org",
        auto_apply_supply=False,
    )
    requester_replies = sent_to(harness, "volunteer@example.org")
    assert any("Read-only status" in body and need_ref in body for body in requester_replies)

    harness.intelligence.set_fixture(
        "query-al",
        _parsed(
            ReportKind.QUERY,
            "Shelter 4",
            EntityType.SHELTER,
            [ParsedReport(resource=Resource.POTABLE_WATER)],
            reference_id=allocation_ref,
        ),
    )
    await deliver(
        harness,
        provider_message_id="msg-query-al",
        subject="Allocation status",
        text=f"query-al QUERY STATUS {allocation_ref}",
        sender="volunteer@example.org",
        auto_apply_supply=False,
    )
    allocation_replies = sent_to(harness, "volunteer@example.org")
    assert any(
        "Read-only status for" in body
        and allocation_ref in body
        and "Reserved quantity" in body
        for body in allocation_replies
    )

    harness.intelligence.set_fixture(
        "query-attack",
        _parsed(
            ReportKind.QUERY,
            "Shelter 4",
            EntityType.SHELTER,
            [ParsedReport(resource=Resource.POTABLE_WATER)],
            reference_id=need_ref,
        ),
    )
    await deliver(
        harness,
        provider_message_id="msg-query-attack",
        subject="Status",
        text=f"query-attack QUERY STATUS {need_ref}",
        sender="attacker@example.org",
        auto_apply_supply=False,
    )
    attack_replies = sent_to(harness, "attacker@example.org")
    assert attack_replies
    assert all("Read-only status" not in body for body in attack_replies)

    async with session_factory() as session:
        need = await session.scalar(sa.select(Need))
        allocation = await session.scalar(sa.select(Allocation))
    assert need is not None and allocation is not None
    assert (need.status, allocation.status) == before


async def test_rematch_job_cannot_touch_another_incidents_need(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    harness = await build_harness(session_factory, incident_id, clock)
    await deliver(
        harness,
        provider_message_id="msg-demo-need",
        subject="Shelter 4 water",
        text="Shelter 4 has 40 people and zero potable water near Gandhi Park.",
        sender="volunteer@example.org",
        auto_apply_supply=False,
    )
    async with session_factory() as session:
        message = await session.scalar(
            sa.select(Message).where(
                Message.provider_message_id == "msg-demo-need"
            )
        )
        assert message is not None
        message_id = message.id
    async with transaction(session_factory) as session:
        other = Incident(key="foreign", name="Foreign", simulation=True)
        session.add(other)
        await session.flush()
        entity = Entity(
            incident_id=other.id,
            name="Foreign Shelter",
            normalized_name="foreign shelter",
            entity_type=EntityType.SHELTER,
        )
        session.add(entity)
        await session.flush()
        foreign_need = Need(
            incident_id=other.id,
            entity_id=entity.id,
            ref="RN-900001",
            resource=Resource.POTABLE_WATER,
            quantity=Decimal("50"),
            unit="l",
            quantity_source=QuantitySource.STATED,
            status=NeedStatus.OPEN,
        )
        session.add(foreign_need)
        await session.flush()
        session.add(
            Job(
                incident_id=incident_id,
                type=JobType.PROCESS_MESSAGE,
                payload={
                    "message_id": str(message_id),
                    "rematch_need_ids": [str(foreign_need.id)],
                },
                status=JobStatus.PENDING,
                dedupe_key="foreign-rematch-test",
                # The column default is real utcnow(); the harness clock is
                # frozen at NOW, so the job must be made due explicitly, as
                # every other direct-Job test does.
                available_at=NOW,
            )
        )

    assert await harness.worker.run_once() == 1
    async with session_factory() as session:
        allocations = list(
            await session.scalars(
                sa.select(Allocation).where(Allocation.incident_id == other.id)
            )
        )
        foreign = await session.get(Need, foreign_need.id)
    assert allocations == []
    assert foreign is not None and foreign.status is NeedStatus.OPEN


async def test_status_query_for_an_unknown_reference_is_truthful(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    harness = await build_harness(session_factory, incident_id, clock)
    harness.intelligence.set_fixture(
        "query-missing",
        _parsed(
            ReportKind.QUERY,
            "Shelter 4",
            EntityType.SHELTER,
            [ParsedReport(resource=Resource.POTABLE_WATER)],
            reference_id="RN-999999",
        ),
    )
    await deliver(
        harness,
        provider_message_id="msg-query-missing",
        subject="Status",
        text="query-missing QUERY STATUS RN-999999",
        sender="seeker@example.org",
        auto_apply_supply=False,
    )
    replies = sent_to(harness, "seeker@example.org")
    assert len(replies) == 1
    assert "No need or allocation with that reference exists" in replies[0]
    assert "no review was opened" in replies[0]
    async with session_factory() as session:
        cases = list(await session.scalars(sa.select(ReviewCase)))
    assert cases == []

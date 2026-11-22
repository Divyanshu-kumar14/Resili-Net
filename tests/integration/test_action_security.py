"""Task 14: scoped single-use action grants and authorization.

Every test drives the real ASGI routes, so GET/POST, session, CSRF, token
consumption, and the database transaction are all in the loop. Fake providers
only replace external email/identity transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.adapters.fakes import FakeAuthProvider, FakeInboxProvider
from resilinet.config import Settings
from resilinet.db import transaction
from resilinet.domain.quantities import QuantitySource
from resilinet.main import Container, create_app
from resilinet.models import (
    ActionGrant,
    Allocation,
    Entity,
    EventLog,
    Incident,
    Job,
    Message,
    Need,
    OutboundEmail,
    ParticipantRole,
    ParticipantRoleKind,
    PendingAction,
    PendingActionKind,
    PendingActionStatus,
    Report,
    ReviewCase,
    ReviewCaseKind,
    ReviewStatus,
    Sender,
    Supply,
)
from resilinet.schemas import (
    AllocationStatus,
    EntityType,
    NeedStatus,
    ReportKind,
    Resource,
    Urgency,
)
from resilinet.security import (
    AUTH_NONCE_COOKIE,
    SESSION_COOKIE_NAME,
    derive_csrf_token,
    hash_token,
)
from resilinet.services.actions import create_action_grant, payload_hash

from .conftest import NOW, Clock

SETTINGS = Settings(
    app_env="test",
    incident_key="demo-incident",
    database_url="sqlite+aiosqlite:///:memory:",
    public_base_url="http://testserver",
    _env_file=None,
)


@dataclass
class Scenario:
    warehouse: Entity
    shelter: Entity
    requester: Sender
    coordinator: Sender
    allocation: Allocation
    need: Need
    supply: Supply
    source_message: Message


def make_app(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    auth: FakeAuthProvider,
) -> FastAPI:
    container = Container(
        settings=SETTINGS,
        session_factory=session_factory,
        inbox=FakeInboxProvider(webhook_secret="secret", now=clock),
        auth=auth,
        clock=clock,
    )
    return create_app(SETTINGS, container)


async def add_sender(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    address: str,
    *,
    role: ParticipantRoleKind | None = None,
    entity_id: UUID | None = None,
    role_incident_id: UUID | None = None,
    verified: bool = True,
) -> Sender:
    async with transaction(session_factory) as session:
        sender = Sender(
            incident_id=incident_id,
            address=address,
            verified=verified,
            verified_at=NOW if verified else None,
        )
        session.add(sender)
        await session.flush()
        if role is not None:
            session.add(
                ParticipantRole(
                    incident_id=role_incident_id or incident_id,
                    sender_id=sender.id,
                    role=role,
                    entity_id=entity_id,
                )
            )
            await session.flush()
        return sender


async def build_scenario(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
    *,
    requester_verified: bool = True,
    allocation_status: AllocationStatus = AllocationStatus.PROPOSED,
    delivered: Decimal = Decimal("0"),
) -> Scenario:
    async with transaction(session_factory) as session:
        warehouse = Entity(
            incident_id=incident_id,
            name="Depot B",
            normalized_name="depot b",
            entity_type=EntityType.DEPOT,
            lat=22.57,
            lon=88.36,
        )
        shelter = Entity(
            incident_id=incident_id,
            name="Shelter 4",
            normalized_name="shelter 4",
            entity_type=EntityType.SHELTER,
            lat=22.58,
            lon=88.37,
        )
        session.add_all([warehouse, shelter])
        await session.flush()

        requester = Sender(
            incident_id=incident_id,
            address="requester@example.org",
            verified=requester_verified,
            verified_at=NOW if requester_verified else None,
        )
        coordinator = Sender(
            incident_id=incident_id,
            address="coordinator@example.org",
            verified=True,
            verified_at=NOW,
        )
        session.add_all([requester, coordinator])
        await session.flush()
        session.add(
            ParticipantRole(
                incident_id=incident_id,
                sender_id=coordinator.id,
                role=ParticipantRoleKind.DEPOT_COORDINATOR,
                entity_id=warehouse.id,
            )
        )
        message = Message(
            incident_id=incident_id,
            sender_id=requester.id,
            provider_message_id="msg-need",
            mailbox_id="mb-1",
            subject="Water",
            body_text="We need water",
            received_at=NOW,
        )
        session.add(message)
        await session.flush()
        report = Report(
            incident_id=incident_id,
            message_id=message.id,
            sender_id=requester.id,
            entity_id=shelter.id,
            kind=ReportKind.NEED,
            resource=Resource.POTABLE_WATER,
            quantity=Decimal("100"),
            unit="l",
            quantity_source=QuantitySource.STATED,
        )
        session.add(report)
        await session.flush()
        need = Need(
            incident_id=incident_id,
            entity_id=shelter.id,
            source_report_id=report.id,
            ref="RN-100001",
            resource=Resource.POTABLE_WATER,
            quantity=Decimal("100"),
            unit="l",
            quantity_source=QuantitySource.STATED,
            headcount=40,
            urgency=Urgency.HIGH,
            status=NeedStatus.OPEN,
            verified=requester_verified,
        )
        supply = Supply(
            incident_id=incident_id,
            entity_id=warehouse.id,
            resource=Resource.POTABLE_WATER,
            on_hand=Decimal("100"),
            unit="l",
        )
        session.add_all([need, supply])
        await session.flush()
        allocation = Allocation(
            incident_id=incident_id,
            need_id=need.id,
            supply_id=supply.id,
            ref="AL-100001",
            resource=Resource.POTABLE_WATER,
            quantity=Decimal("100"),
            unit="l",
            delivered_quantity=delivered,
            distance_km=Decimal("2.5"),
            status=allocation_status,
            critic_verdict="approved",
        )
        session.add(allocation)
        await session.flush()
        return Scenario(
            warehouse=warehouse,
            shelter=shelter,
            requester=requester,
            coordinator=coordinator,
            allocation=allocation,
            need=need,
            supply=supply,
            source_message=message,
        )


async def issue_grant(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    kind: PendingActionKind,
    incident_id: UUID,
    sender_id: UUID,
    entity_id: UUID | None = None,
    need_id: UUID | None = None,
    allocation_id: UUID | None = None,
    pending_action_id: UUID | None = None,
    digest: str | None = None,
    ttl: timedelta = timedelta(minutes=30),
) -> str:
    async with transaction(session_factory) as session:
        _grant, token = await create_action_grant(
            session,
            kind=kind,
            incident_id=incident_id,
            sender_id=sender_id,
            entity_id=entity_id,
            need_id=need_id,
            allocation_id=allocation_id,
            pending_action_id=pending_action_id,
            payload_hash=digest,
            now=clock.now,
            ttl=ttl,
        )
        return token


async def make_pending_action(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    scenario: Scenario,
    kind: PendingActionKind,
    payload: dict[str, object],
    sender_id: UUID | None = None,
    entity_id: UUID | None = None,
) -> PendingAction:
    async with transaction(session_factory) as session:
        pending = PendingAction(
            kind=kind,
            incident_id=scenario.need.incident_id,
            sender_id=sender_id or scenario.coordinator.id,
            source_message_id=scenario.source_message.id,
            source_report_id=scenario.need.source_report_id,
            entity_id=entity_id,
            need_id=scenario.need.id if kind is not PendingActionKind.UPDATE_SUPPLY else None,
            allocation_id=(
                scenario.allocation.id
                if kind
                in (
                    PendingActionKind.DELIVER_ALLOCATION,
                    PendingActionKind.CANCEL_ALLOCATION,
                )
                else None
            ),
            payload_json=payload,
            payload_hash=payload_hash(payload),
            status=PendingActionStatus.PENDING,
        )
        session.add(pending)
        await session.flush()
        return pending


async def login(
    client: httpx.AsyncClient,
    auth: FakeAuthProvider,
    address: str,
) -> str:
    response = await client.post("/operator/login", data={"email": address})
    assert response.status_code == 200
    state = auth.sent[-1].state_id
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


async def grant_row(
    session_factory: async_sessionmaker[AsyncSession], token: str
) -> ActionGrant:
    async with session_factory() as session:
        row = await session.scalar(
            sa.select(ActionGrant).where(
                ActionGrant.token_hash == hash_token(token)
            )
        )
        assert row is not None
        return row


async def test_get_action_page_never_mutates(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(session_factory, incident_id, clock)
    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.CONFIRM_ALLOCATION,
        incident_id=incident_id,
        sender_id=scenario.coordinator.id,
        entity_id=scenario.warehouse.id,
        need_id=scenario.need.id,
        allocation_id=scenario.allocation.id,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await login(client, auth, scenario.coordinator.address)
        page = await client.get(f"/actions/{token}")
        assert page.status_code == 200
        assert "Confirm allocation AL-100001" in page.text
        assert "csrf_token" in page.text
        assert "no-referrer" in page.headers.get("referrer-policy", "")

    async with session_factory() as session:
        allocation = await session.get(Allocation, scenario.allocation.id)
        grant = await grant_row(session_factory, token)
    assert allocation is not None and allocation.status is AllocationStatus.PROPOSED
    assert grant.consumed_at is None


async def test_invalid_expired_and_consumed_links_are_generic(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(session_factory, incident_id, clock)
    expired = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.CONFIRM_ALLOCATION,
        incident_id=incident_id,
        sender_id=scenario.coordinator.id,
        entity_id=scenario.warehouse.id,
        need_id=scenario.need.id,
        allocation_id=scenario.allocation.id,
        ttl=timedelta(minutes=-1),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await login(client, auth, scenario.coordinator.address)
        unknown = await client.get("/actions/not-a-real-token")
        assert unknown.status_code == 404
        assert "Depot B" not in unknown.text
        expired_page = await client.get(f"/actions/{expired}")
        assert expired_page.status_code == 404

    async with session_factory() as session:
        allocation = await session.get(Allocation, scenario.allocation.id)
    assert allocation is not None and allocation.status is AllocationStatus.PROPOSED


async def test_missing_session_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(session_factory, incident_id, clock)
    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.CONFIRM_ALLOCATION,
        incident_id=incident_id,
        sender_id=scenario.coordinator.id,
        entity_id=scenario.warehouse.id,
        need_id=scenario.need.id,
        allocation_id=scenario.allocation.id,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        get_page = await client.get(f"/actions/{token}")
        post_page = await client.post(
            f"/actions/{token}", data={"csrf_token": "anything"}
        )
        assert get_page.status_code == 401
        assert post_page.status_code == 401

    async with session_factory() as session:
        allocation = await session.get(Allocation, scenario.allocation.id)
        grant = await grant_row(session_factory, token)
    assert allocation is not None and allocation.status is AllocationStatus.PROPOSED
    assert grant.consumed_at is None


async def test_forwarded_link_to_another_verified_sender_is_denied(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(session_factory, incident_id, clock)
    other = await add_sender(
        session_factory, incident_id, "other@example.org", verified=True
    )
    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.CONFIRM_ALLOCATION,
        incident_id=incident_id,
        sender_id=scenario.coordinator.id,
        entity_id=scenario.warehouse.id,
        need_id=scenario.need.id,
        allocation_id=scenario.allocation.id,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_token = await login(client, auth, other.address)
        get_page = await client.get(f"/actions/{token}")
        post_page = await client.post(
            f"/actions/{token}", data={"csrf_token": csrf_for(session_token)}
        )
        assert get_page.status_code == 403
        assert post_page.status_code == 403

    async with session_factory() as session:
        allocation = await session.get(Allocation, scenario.allocation.id)
        grant = await grant_row(session_factory, token)
    assert allocation is not None and allocation.status is AllocationStatus.PROPOSED
    assert grant.consumed_at is None


async def test_cross_incident_role_is_not_enough(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(session_factory, incident_id, clock)
    async with transaction(session_factory) as session:
        other_incident = Incident(
            key="other-incident", name="Other Incident", simulation=True
        )
        session.add(other_incident)
        await session.flush()
        other_id = other_incident.id
    # The sender has depot authority only in another incident.
    scoped = await add_sender(
        session_factory,
        incident_id,
        "scoped@example.org",
        role=ParticipantRoleKind.DEPOT_COORDINATOR,
        entity_id=scenario.warehouse.id,
        role_incident_id=other_id,
    )
    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.CONFIRM_ALLOCATION,
        incident_id=incident_id,
        sender_id=scoped.id,
        entity_id=scenario.warehouse.id,
        need_id=scenario.need.id,
        allocation_id=scenario.allocation.id,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_token = await login(client, auth, scoped.address)
        response = await client.post(
            f"/actions/{token}", data={"csrf_token": csrf_for(session_token)}
        )
        assert response.status_code == 403
    async with session_factory() as session:
        allocation = await session.get(Allocation, scenario.allocation.id)
    assert allocation is not None and allocation.status is AllocationStatus.PROPOSED


async def test_requester_cannot_confirm_a_depot_allocation(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(session_factory, incident_id, clock)
    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.CONFIRM_ALLOCATION,
        incident_id=incident_id,
        sender_id=scenario.requester.id,
        entity_id=scenario.warehouse.id,
        need_id=scenario.need.id,
        allocation_id=scenario.allocation.id,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_token = await login(client, auth, scenario.requester.address)
        response = await client.post(
            f"/actions/{token}", data={"csrf_token": csrf_for(session_token)}
        )
        assert response.status_code == 403
    async with session_factory() as session:
        allocation = await session.get(Allocation, scenario.allocation.id)
    assert allocation is not None and allocation.status is AllocationStatus.PROPOSED


async def test_unrelated_depot_cannot_overwrite_stock(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(session_factory, incident_id, clock)
    async with transaction(session_factory) as session:
        other_depot = Entity(
            incident_id=incident_id,
            name="Depot A",
            normalized_name="depot a",
            entity_type=EntityType.DEPOT,
            lat=22.60,
            lon=88.40,
        )
        session.add(other_depot)
        await session.flush()
        other_depot_id = other_depot.id
    coordinator_a = await add_sender(
        session_factory,
        incident_id,
        "coordinator-a@example.org",
        role=ParticipantRoleKind.DEPOT_COORDINATOR,
        entity_id=other_depot_id,
    )
    payload = {
        "entity_id": str(scenario.warehouse.id),
        "resource": Resource.POTABLE_WATER.value,
        "quantity": "500",
        "unit": "l",
        "report_id": None,
    }
    pending = await make_pending_action(
        session_factory,
        clock,
        scenario=scenario,
        kind=PendingActionKind.UPDATE_SUPPLY,
        payload=payload,
        sender_id=coordinator_a.id,
        entity_id=scenario.warehouse.id,
    )
    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.UPDATE_SUPPLY,
        incident_id=incident_id,
        sender_id=coordinator_a.id,
        entity_id=scenario.warehouse.id,
        pending_action_id=pending.id,
        digest=pending.payload_hash,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_token = await login(client, auth, coordinator_a.address)
        response = await client.post(
            f"/actions/{token}", data={"csrf_token": csrf_for(session_token)}
        )
        assert response.status_code == 403
    async with session_factory() as session:
        supply = await session.get(Supply, scenario.supply.id)
    assert supply is not None and supply.on_hand == Decimal("100")


async def test_grant_cannot_target_another_incidents_allocation(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(session_factory, incident_id, clock)
    async with transaction(session_factory) as session:
        other = Incident(key="foreign-incident", name="Foreign", simulation=True)
        session.add(other)
        await session.flush()
        warehouse = Entity(
            incident_id=other.id,
            name="Foreign Depot",
            normalized_name="foreign depot",
            entity_type=EntityType.DEPOT,
            lat=22.4,
            lon=88.2,
        )
        shelter = Entity(
            incident_id=other.id,
            name="Foreign Shelter",
            normalized_name="foreign shelter",
            entity_type=EntityType.SHELTER,
            lat=22.41,
            lon=88.21,
        )
        session.add_all([warehouse, shelter])
        await session.flush()
        requester = Sender(
            incident_id=other.id, address="foreign@example.org", verified=True
        )
        session.add(requester)
        await session.flush()
        foreign_need = Need(
            incident_id=other.id,
            entity_id=shelter.id,
            ref="RN-900001",
            resource=Resource.POTABLE_WATER,
            quantity=Decimal("50"),
            unit="l",
            quantity_source=QuantitySource.STATED,
            status=NeedStatus.OPEN,
        )
        foreign_supply = Supply(
            incident_id=other.id,
            entity_id=warehouse.id,
            resource=Resource.POTABLE_WATER,
            on_hand=Decimal("50"),
            unit="l",
        )
        session.add_all([foreign_need, foreign_supply])
        await session.flush()
        foreign_allocation = Allocation(
            incident_id=other.id,
            need_id=foreign_need.id,
            supply_id=foreign_supply.id,
            ref="AL-900001",
            resource=Resource.POTABLE_WATER,
            quantity=Decimal("50"),
            unit="l",
            delivered_quantity=Decimal("0"),
            distance_km=Decimal("1"),
            status=AllocationStatus.PROPOSED,
            critic_verdict="approved",
        )
        session.add(foreign_allocation)
        await session.flush()
        foreign_id = foreign_allocation.id

    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.CONFIRM_ALLOCATION,
        incident_id=incident_id,
        sender_id=scenario.coordinator.id,
        entity_id=scenario.warehouse.id,
        need_id=scenario.need.id,
        allocation_id=foreign_id,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_token = await login(client, auth, scenario.coordinator.address)
        response = await client.post(
            f"/actions/{token}", data={"csrf_token": csrf_for(session_token)}
        )
        assert response.status_code == 403
    async with session_factory() as session:
        foreign = await session.get(Allocation, foreign_id)
    assert foreign is not None and foreign.status is AllocationStatus.PROPOSED


async def test_requester_cannot_report_another_senders_allocation(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(
        session_factory, incident_id, clock, allocation_status=AllocationStatus.CONFIRMED
    )
    other = await add_sender(
        session_factory, incident_id, "other-requester@example.org", verified=True
    )
    payload = {
        "quantity": "10",
        "unit": "l",
    }
    pending = await make_pending_action(
        session_factory,
        clock,
        scenario=scenario,
        kind=PendingActionKind.DELIVER_ALLOCATION,
        payload=payload,
        sender_id=other.id,
    )
    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.DELIVER_ALLOCATION,
        incident_id=incident_id,
        sender_id=other.id,
        need_id=scenario.need.id,
        allocation_id=scenario.allocation.id,
        pending_action_id=pending.id,
        digest=pending.payload_hash,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_token = await login(client, auth, other.address)
        response = await client.post(
            f"/actions/{token}", data={"csrf_token": csrf_for(session_token)}
        )
        assert response.status_code == 403
    async with session_factory() as session:
        allocation = await session.get(Allocation, scenario.allocation.id)
    assert allocation is not None and allocation.delivered_quantity == Decimal("0")


async def test_csrf_absence_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(session_factory, incident_id, clock)
    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.CONFIRM_ALLOCATION,
        incident_id=incident_id,
        sender_id=scenario.coordinator.id,
        entity_id=scenario.warehouse.id,
        need_id=scenario.need.id,
        allocation_id=scenario.allocation.id,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await login(client, auth, scenario.coordinator.address)
        response = await client.post(f"/actions/{token}", data={})
        assert response.status_code == 403
    async with session_factory() as session:
        allocation = await session.get(Allocation, scenario.allocation.id)
        grant = await grant_row(session_factory, token)
    assert allocation is not None and allocation.status is AllocationStatus.PROPOSED
    assert grant.consumed_at is None


async def test_double_submit_mutates_once(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(session_factory, incident_id, clock)
    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.CONFIRM_ALLOCATION,
        incident_id=incident_id,
        sender_id=scenario.coordinator.id,
        entity_id=scenario.warehouse.id,
        need_id=scenario.need.id,
        allocation_id=scenario.allocation.id,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_token = await login(client, auth, scenario.coordinator.address)
        csrf = csrf_for(session_token)
        first = await client.post(f"/actions/{token}", data={"csrf_token": csrf})
        second = await client.post(f"/actions/{token}", data={"csrf_token": csrf})
        assert first.status_code == 200
        assert second.status_code == 404
    async with session_factory() as session:
        allocation = await session.get(Allocation, scenario.allocation.id)
        events = list(
            await session.scalars(
                sa.select(EventLog).where(EventLog.event_type == "allocation.confirmed")
            )
        )
        grant = await grant_row(session_factory, token)
    assert allocation is not None and allocation.status is AllocationStatus.CONFIRMED
    assert len(events) == 1
    assert grant.consumed_at is not None


async def test_invalid_delivery_quantity_is_rejected_without_consuming(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(
        session_factory, incident_id, clock, allocation_status=AllocationStatus.CONFIRMED
    )
    payload = {"quantity": "500", "unit": "l"}
    pending = await make_pending_action(
        session_factory,
        clock,
        scenario=scenario,
        kind=PendingActionKind.DELIVER_ALLOCATION,
        payload=payload,
        sender_id=scenario.requester.id,
    )
    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.DELIVER_ALLOCATION,
        incident_id=incident_id,
        sender_id=scenario.requester.id,
        need_id=scenario.need.id,
        allocation_id=scenario.allocation.id,
        pending_action_id=pending.id,
        digest=pending.payload_hash,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_token = await login(client, auth, scenario.requester.address)
        response = await client.post(
            f"/actions/{token}", data={"csrf_token": csrf_for(session_token)}
        )
        assert response.status_code == 400
        assert "No change was made" in response.text
    async with session_factory() as session:
        allocation = await session.get(Allocation, scenario.allocation.id)
        grant = await grant_row(session_factory, token)
    assert allocation is not None and allocation.delivered_quantity == Decimal("0")
    assert grant.consumed_at is None


async def test_pending_payload_hash_mismatch_is_denied(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(
        session_factory, incident_id, clock, allocation_status=AllocationStatus.CONFIRMED
    )
    payload = {"quantity": "10", "unit": "l"}
    pending = await make_pending_action(
        session_factory,
        clock,
        scenario=scenario,
        kind=PendingActionKind.DELIVER_ALLOCATION,
        payload=payload,
        sender_id=scenario.requester.id,
    )
    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.DELIVER_ALLOCATION,
        incident_id=incident_id,
        sender_id=scenario.requester.id,
        need_id=scenario.need.id,
        allocation_id=scenario.allocation.id,
        pending_action_id=pending.id,
        digest=pending.payload_hash,
    )
    async with transaction(session_factory) as session:
        row = await session.get(PendingAction, pending.id)
        assert row is not None
        row.payload_json = {"quantity": "99", "unit": "l"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_token = await login(client, auth, scenario.requester.address)
        response = await client.post(
            f"/actions/{token}", data={"csrf_token": csrf_for(session_token)}
        )
        assert response.status_code == 403
    async with session_factory() as session:
        allocation = await session.get(Allocation, scenario.allocation.id)
    assert allocation is not None and allocation.delivered_quantity == Decimal("0")


async def test_success_writes_audit_and_outbox(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(session_factory, incident_id, clock)
    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.CONFIRM_ALLOCATION,
        incident_id=incident_id,
        sender_id=scenario.coordinator.id,
        entity_id=scenario.warehouse.id,
        need_id=scenario.need.id,
        allocation_id=scenario.allocation.id,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_token = await login(client, auth, scenario.coordinator.address)
        response = await client.post(
            f"/actions/{token}", data={"csrf_token": csrf_for(session_token)}
        )
        assert response.status_code == 200
        assert "Allocation confirmed" in response.text
        assert "not a shipment" in response.text
    async with session_factory() as session:
        grant = await grant_row(session_factory, token)
        consumed = await session.scalar(
            sa.select(EventLog).where(EventLog.event_type == "action.consumed")
        )
        confirmed = await session.scalar(
            sa.select(EventLog).where(EventLog.event_type == "allocation.confirmed")
        )
        outbox = await session.scalar(
            sa.select(OutboundEmail).where(
                OutboundEmail.kind == f"action_result:{grant.id}"
            )
        )
    assert consumed is not None
    assert confirmed is not None
    assert outbox is not None
    assert outbox.to_address == scenario.requester.address
    assert "/actions/" not in outbox.body_text


async def test_unverified_requester_confirmation_waits_for_trust(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(
        session_factory, incident_id, clock, requester_verified=False
    )
    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.CONFIRM_ALLOCATION,
        incident_id=incident_id,
        sender_id=scenario.coordinator.id,
        entity_id=scenario.warehouse.id,
        need_id=scenario.need.id,
        allocation_id=scenario.allocation.id,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_token = await login(client, auth, scenario.coordinator.address)
        csrf = csrf_for(session_token)
        denied = await client.post(f"/actions/{token}", data={"csrf_token": csrf})
        assert denied.status_code == 403
        assert "must verify" in denied.text

        async with transaction(session_factory) as session:
            requester = await session.get(Sender, scenario.requester.id)
            assert requester is not None
            requester.verified = True
            requester.verified_at = clock.now
        allowed = await client.post(f"/actions/{token}", data={"csrf_token": csrf})
        assert allowed.status_code == 200
    async with session_factory() as session:
        allocation = await session.get(Allocation, scenario.allocation.id)
    assert allocation is not None and allocation.status is AllocationStatus.CONFIRMED


async def test_recorded_operator_approval_allows_unverified_requester_confirmation(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(
        session_factory, incident_id, clock, requester_verified=False
    )
    async with transaction(session_factory) as session:
        session.add(
            ReviewCase(
                incident_id=incident_id,
                kind=ReviewCaseKind.MEDICAL,
                status=ReviewStatus.RESOLVED,
                need_id=scenario.need.id,
                reason="approved by operator",
                resolution="APPROVE_PROPOSAL",
                resolved_at=NOW,
            )
        )
    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.CONFIRM_ALLOCATION,
        incident_id=incident_id,
        sender_id=scenario.coordinator.id,
        entity_id=scenario.warehouse.id,
        need_id=scenario.need.id,
        allocation_id=scenario.allocation.id,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_token = await login(client, auth, scenario.coordinator.address)
        response = await client.post(
            f"/actions/{token}", data={"csrf_token": csrf_for(session_token)}
        )
        assert response.status_code == 200
    async with session_factory() as session:
        allocation = await session.get(Allocation, scenario.allocation.id)
    assert allocation is not None and allocation.status is AllocationStatus.CONFIRMED


async def test_stock_payload_entity_must_match_the_pending_target(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(session_factory, incident_id, clock)
    async with transaction(session_factory) as session:
        other = Entity(
            incident_id=incident_id,
            name="Depot C",
            normalized_name="depot c",
            entity_type=EntityType.DEPOT,
            lat=22.52,
            lon=88.32,
        )
        session.add(other)
        await session.flush()
        other_id = other.id
    payload = {
        "entity_id": str(other_id),
        "resource": Resource.POTABLE_WATER.value,
        "quantity": "900",
        "unit": "l",
        "report_id": None,
    }
    pending = await make_pending_action(
        session_factory,
        clock,
        scenario=scenario,
        kind=PendingActionKind.UPDATE_SUPPLY,
        payload=payload,
        sender_id=scenario.coordinator.id,
        entity_id=scenario.warehouse.id,
    )
    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.UPDATE_SUPPLY,
        incident_id=incident_id,
        sender_id=scenario.coordinator.id,
        entity_id=None,
        pending_action_id=pending.id,
        digest=pending.payload_hash,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_token = await login(client, auth, scenario.coordinator.address)
        response = await client.post(
            f"/actions/{token}", data={"csrf_token": csrf_for(session_token)}
        )
        assert response.status_code == 403
    async with session_factory() as session:
        supply = await session.get(Supply, scenario.supply.id)
        pending_row = await session.get(PendingAction, pending.id)
    assert supply is not None and supply.on_hand == Decimal("100")
    assert pending_row is not None and pending_row.status is PendingActionStatus.PENDING


async def test_confirm_grant_for_a_cancelled_allocation_is_generic_404(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(session_factory, incident_id, clock)
    async with transaction(session_factory) as session:
        allocation = await session.get(Allocation, scenario.allocation.id)
        assert allocation is not None
        allocation.status = AllocationStatus.CANCELLED
    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.CONFIRM_ALLOCATION,
        incident_id=incident_id,
        sender_id=scenario.coordinator.id,
        entity_id=scenario.warehouse.id,
        need_id=scenario.need.id,
        allocation_id=scenario.allocation.id,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_token = await login(client, auth, scenario.coordinator.address)
        get_page = await client.get(f"/actions/{token}")
        post_page = await client.post(
            f"/actions/{token}", data={"csrf_token": csrf_for(session_token)}
        )
        assert get_page.status_code == 404
        assert post_page.status_code == 404
    async with session_factory() as session:
        allocation = await session.get(Allocation, scenario.allocation.id)
        grant = await grant_row(session_factory, token)
    assert allocation is not None and allocation.status is AllocationStatus.CANCELLED
    assert grant.consumed_at is None


async def test_open_medical_review_blocks_confirmation_until_resolved(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(session_factory, incident_id, clock)
    async with transaction(session_factory) as session:
        case = ReviewCase(
            incident_id=incident_id,
            kind=ReviewCaseKind.MEDICAL,
            status=ReviewStatus.OPEN,
            need_id=scenario.need.id,
            reason="medical corroboration",
        )
        session.add(case)
        await session.flush()
        case_id = case.id
    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.CONFIRM_ALLOCATION,
        incident_id=incident_id,
        sender_id=scenario.coordinator.id,
        entity_id=scenario.warehouse.id,
        need_id=scenario.need.id,
        allocation_id=scenario.allocation.id,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_token = await login(client, auth, scenario.coordinator.address)
        csrf = csrf_for(session_token)
        denied = await client.post(f"/actions/{token}", data={"csrf_token": csrf})
        assert denied.status_code == 403
        assert "medical review" in denied.text
        async with transaction(session_factory) as session:
            case = await session.get(ReviewCase, case_id)
            assert case is not None
            case.status = ReviewStatus.RESOLVED
            case.resolution = "APPROVE_PROPOSAL"
            case.resolved_at = clock.now
        allowed = await client.post(f"/actions/{token}", data={"csrf_token": csrf})
        assert allowed.status_code == 200
    async with session_factory() as session:
        allocation = await session.get(Allocation, scenario.allocation.id)
    assert allocation is not None and allocation.status is AllocationStatus.CONFIRMED


async def test_malformed_delivery_payload_preview_is_a_generic_404(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(
        session_factory, incident_id, clock, allocation_status=AllocationStatus.CONFIRMED
    )
    payload = {"unit": "l"}  # missing quantity on purpose
    pending = await make_pending_action(
        session_factory,
        clock,
        scenario=scenario,
        kind=PendingActionKind.DELIVER_ALLOCATION,
        payload=payload,
        sender_id=scenario.requester.id,
    )
    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.DELIVER_ALLOCATION,
        incident_id=incident_id,
        sender_id=scenario.requester.id,
        need_id=scenario.need.id,
        allocation_id=scenario.allocation.id,
        pending_action_id=pending.id,
        digest=pending.payload_hash,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await login(client, auth, scenario.requester.address)
        response = await client.get(f"/actions/{token}")
        assert response.status_code == 404
    async with session_factory() as session:
        allocation = await session.get(Allocation, scenario.allocation.id)
    assert allocation is not None and allocation.delivered_quantity == Decimal("0")


async def test_supply_snapshot_applies_only_on_post_and_schedules_rematch(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    scenario = await build_scenario(session_factory, incident_id, clock)
    async with transaction(session_factory) as session:
        allocation = await session.get(Allocation, scenario.allocation.id)
        assert allocation is not None
        allocation.quantity = Decimal("40")
    payload = {
        "entity_id": str(scenario.warehouse.id),
        "resource": Resource.POTABLE_WATER.value,
        "quantity": "300",
        "unit": "l",
        "report_id": None,
    }
    pending = await make_pending_action(
        session_factory,
        clock,
        scenario=scenario,
        kind=PendingActionKind.UPDATE_SUPPLY,
        payload=payload,
        sender_id=scenario.coordinator.id,
        entity_id=scenario.warehouse.id,
    )
    token = await issue_grant(
        session_factory,
        clock,
        kind=PendingActionKind.UPDATE_SUPPLY,
        incident_id=incident_id,
        sender_id=scenario.coordinator.id,
        entity_id=scenario.warehouse.id,
        pending_action_id=pending.id,
        digest=pending.payload_hash,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_token = await login(client, auth, scenario.coordinator.address)
        page = await client.get(f"/actions/{token}")
        assert page.status_code == 200
        async with session_factory() as session:
            supply = await session.get(Supply, scenario.supply.id)
        assert supply is not None and supply.on_hand == Decimal("100")

        response = await client.post(
            f"/actions/{token}", data={"csrf_token": csrf_for(session_token)}
        )
        assert response.status_code == 200
    async with session_factory() as session:
        supply = await session.get(Supply, scenario.supply.id)
        rematch = await session.scalar(
            sa.select(Job).where(Job.dedupe_key == f"rematch:{pending.id}")
        )
        pending_row = await session.get(PendingAction, pending.id)
    assert supply is not None and supply.on_hand == Decimal("300")
    assert rematch is not None
    assert rematch.payload["rematch_need_ids"] == [str(scenario.need.id)]
    assert pending_row is not None and pending_row.status is PendingActionStatus.CONSUMED

"""Task 16: operator login, review queue, escaped detail, and resolutions."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import httpx
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.adapters.fakes import (
    FakeAuthProvider,
    FakeGeocoder,
    FakeInboxProvider,
    FakeIntelligenceProvider,
)
from resilinet.config import Settings
from resilinet.db import transaction
from resilinet.domain.quantities import QuantitySource
from resilinet.main import Container, create_app
from resilinet.models import (
    Allocation,
    Entity,
    EventLog,
    Incident,
    Job,
    JobType,
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
from resilinet.services.actions import payload_hash
from resilinet.services.extraction import ExtractionService
from resilinet.services.notifications import NotificationService
from resilinet.workers.handlers import MessageJobHandler
from resilinet.workers.runner import DurableWorker

from .conftest import NOW, Clock

SETTINGS = Settings(
    app_env="test",
    incident_key="demo-incident",
    database_url="sqlite+aiosqlite:///:memory:",
    public_base_url="http://testserver",
    _env_file=None,
)


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


async def add_operator(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    address: str = "ops@example.org",
) -> Sender:
    async with transaction(session_factory) as session:
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
                role=ParticipantRoleKind.OPERATOR,
                entity_id=None,
            )
        )
        await session.flush()
        return sender


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


async def build_case(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
    *,
    kind: ReviewCaseKind,
    need_quantity: Decimal = Decimal("100"),
    delivered: Decimal = Decimal("0"),
    allocation_status: AllocationStatus = AllocationStatus.PROPOSED,
    with_supply: bool = False,
    needs_human_review: bool = False,
    raw_text: str = "We need water at Shelter 4",
    payload: dict[str, object] | None = None,
) -> dict[str, UUID]:
    async with transaction(session_factory) as session:
        warehouse = await session.scalar(
            sa.select(Entity).where(
                Entity.incident_id == incident_id,
                Entity.normalized_name == "depot b",
            )
        )
        if warehouse is None:
            warehouse = Entity(
                incident_id=incident_id,
                name="Depot B",
                normalized_name="depot b",
                entity_type=EntityType.DEPOT,
                lat=22.50,
                lon=88.30,
            )
            session.add(warehouse)
        shelter = await session.scalar(
            sa.select(Entity).where(
                Entity.incident_id == incident_id,
                Entity.normalized_name == "shelter 4",
            )
        )
        if shelter is None:
            shelter = Entity(
                incident_id=incident_id,
                name="Shelter 4",
                normalized_name="shelter 4",
                entity_type=EntityType.SHELTER,
                lat=22.58,
                lon=88.37,
            )
            session.add(shelter)
        await session.flush()
        requester = Sender(
            incident_id=incident_id,
            address=f"requester-{kind.value}@example.org",
            verified=False,
        )
        session.add(requester)
        await session.flush()
        message = Message(
            incident_id=incident_id,
            sender_id=requester.id,
            provider_message_id=f"msg-{kind.value}",
            mailbox_id="mb-1",
            subject="Water",
            body_text="body",
            raw_text=raw_text,
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
            quantity=need_quantity,
            unit="l",
            quantity_source=QuantitySource.STATED,
        )
        session.add(report)
        await session.flush()
        need = Need(
            incident_id=incident_id,
            entity_id=shelter.id,
            source_report_id=report.id,
            ref=f"RN-2{len(kind.value):05d}",
            resource=Resource.POTABLE_WATER,
            quantity=need_quantity,
            unit="l",
            quantity_source=QuantitySource.STATED,
            headcount=40,
            status=(
                NeedStatus.PARTIAL
                if delivered > 0
                else NeedStatus.OPEN
            ),
            verified=False,
            needs_human_review=needs_human_review,
        )
        session.add(need)
        await session.flush()
        allocation_id: UUID | None = None
        supply_id: UUID | None = None
        if with_supply or delivered > 0 or allocation_status is not AllocationStatus.PROPOSED:
            supply = Supply(
                incident_id=incident_id,
                entity_id=warehouse.id,
                resource=Resource.POTABLE_WATER,
                on_hand=Decimal("500"),
                unit="l",
            )
            session.add(supply)
            await session.flush()
            supply_id = supply.id
            allocation = Allocation(
                incident_id=incident_id,
                need_id=need.id,
                supply_id=supply.id,
                ref="AL-200001",
                resource=Resource.POTABLE_WATER,
                quantity=need_quantity,
                unit="l",
                delivered_quantity=delivered,
                distance_km=Decimal("5"),
                status=allocation_status,
                critic_verdict="approved",
            )
            session.add(allocation)
            await session.flush()
            allocation_id = allocation.id
        case = ReviewCase(
            incident_id=incident_id,
            kind=kind,
            status=ReviewStatus.OPEN,
            source_message_id=message.id,
            report_id=report.id,
            need_id=need.id,
            allocation_id=allocation_id,
            reason=f"{kind.value} review",
            payload=dict(payload or {}),
        )
        session.add(case)
        await session.flush()
        return {
            "case_id": case.id,
            "need_id": need.id,
            "allocation_id": allocation_id,
            "supply_id": supply_id,
            "message_id": message.id,
            "requester_id": requester.id,
            "shelter_id": shelter.id,
            "warehouse_id": warehouse.id,
        }


async def test_operator_login_queue_and_detail_journey(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    await add_operator(session_factory, incident_id)
    ids = await build_case(
        session_factory, incident_id, clock, kind=ReviewCaseKind.MEDICAL
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await login(client, auth, session_factory, "ops@example.org")
        queue = await client.get("/operator/reviews")
        assert queue.status_code == 200
        assert "medical" in queue.text
        assert "Sign out" in queue.text
        detail = await client.get(f"/operator/reviews/{ids['case_id']}")
        assert detail.status_code == 200
        assert "Review case" in detail.text
        assert "Approve and re-run matching" in detail.text


async def test_only_operators_and_only_their_incident_can_read_cases(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    ids = await build_case(
        session_factory, incident_id, clock, kind=ReviewCaseKind.MEDICAL
    )
    async with transaction(session_factory) as session:
        other_incident = Incident(
            key="other-incident", name="Other Incident", simulation=True
        )
        session.add(other_incident)
        await session.flush()
        other_case = ReviewCase(
            incident_id=other_incident.id,
            kind=ReviewCaseKind.MEDICAL,
            status=ReviewStatus.OPEN,
            reason="other incident",
        )
        session.add(other_case)
        await session.flush()
        other_case_id = other_case.id
        ordinary = Sender(
            incident_id=incident_id, address="ordinary@example.org", verified=True
        )
        session.add(ordinary)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await login(client, auth, session_factory, "ordinary@example.org")
        assert (await client.get("/operator/reviews")).status_code == 403

    await add_operator(session_factory, incident_id)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await login(client, auth, session_factory, "ops@example.org")
        assert (await client.get(f"/operator/reviews/{ids['case_id']}")).status_code == 200
        # Another incident's case is not readable even with an operator role.
        assert (await client.get(f"/operator/reviews/{other_case_id}")).status_code == 404


async def test_case_detail_escapes_untrusted_excerpt_and_payload(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    await add_operator(session_factory, incident_id)
    ids = await build_case(
        session_factory,
        incident_id,
        clock,
        kind=ReviewCaseKind.OTHER,
        raw_text='<script>alert("x")</script>',
        payload={"note": "<img src=x onerror=alert(1)>"},
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await login(client, auth, session_factory, "ops@example.org")
        detail = await client.get(f"/operator/reviews/{ids['case_id']}")
        assert detail.status_code == 200
        assert "&lt;script&gt;" in detail.text
        assert "<script>alert" not in detail.text
        assert "&lt;img" in detail.text
        assert "<img src=x" not in detail.text


async def test_medical_detail_starts_with_no_stock_promise(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    await add_operator(session_factory, incident_id)
    ids = await build_case(
        session_factory,
        incident_id,
        clock,
        kind=ReviewCaseKind.MEDICAL,
        needs_human_review=True,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await login(client, auth, session_factory, "ops@example.org")
        detail = await client.get(f"/operator/reviews/{ids['case_id']}")
        assert detail.status_code == 200
        assert "No stock is promised" in detail.text
        async with session_factory() as session:
            allocations = list(await session.scalars(sa.select(Allocation)))
        assert allocations == []


async def test_approve_clears_the_block_and_runs_normal_reservation(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    await add_operator(session_factory, incident_id)
    ids = await build_case(
        session_factory,
        incident_id,
        clock,
        kind=ReviewCaseKind.MEDICAL,
        needs_human_review=True,
        with_supply=True,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await login(client, auth, session_factory, "ops@example.org")
        response = await client.post(
            f"/operator/reviews/{ids['case_id']}/resolve",
            data={"csrf_token": csrf_for(token), "resolution": "APPROVE_PROPOSAL"},
            follow_redirects=False,
        )
        assert response.status_code == 303
    async with session_factory() as session:
        case = await session.get(ReviewCase, ids["case_id"])
        need = await session.get(Need, ids["need_id"])
        job = await session.scalar(
            sa.select(Job).where(Job.dedupe_key == f"review-rematch:{ids['case_id']}")
        )
    assert case is not None and case.status is ReviewStatus.RESOLVED
    assert case.resolution == "APPROVE_PROPOSAL"
    assert need is not None and need.needs_human_review is False
    assert job is not None

    inbox = FakeInboxProvider(webhook_secret="secret", now=clock)
    await inbox.provision_inbox("demo-incident")
    intelligence = FakeIntelligenceProvider(
        {
            "unused": ParsedEmail(
                context=ReportContext(
                    kind=ReportKind.NEED,
                    confidence=FieldConfidence(location=0.9, items=0.9, headcount=0.9),
                ),
                reports=[ParsedReport(resource=Resource.POTABLE_WATER)],
            )
        }
    )
    handler = MessageJobHandler(
        session_factory=session_factory,
        inbox=inbox,
        extraction=ExtractionService(
            incident_id,
            intelligence,
            FakeGeocoder(
                {"Gandhi Park": (Decimal("22.5"), Decimal("88.3"))}
            ),
        ),
        notifications=NotificationService(),
        intelligence=intelligence,
        clock=clock,
    )
    worker = DurableWorker(
        session_factory,
        {JobType.PROCESS_MESSAGE: handler, JobType.FETCH_CONTENT: handler},
        clock=clock,
        jitter=lambda: 0.0,
    )
    assert await worker.run_once() == 1
    async with session_factory() as session:
        allocation = await session.scalar(sa.select(Allocation))
    assert allocation is not None and allocation.status is AllocationStatus.PROPOSED


async def test_quantity_revision_below_delivered_total_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    await add_operator(session_factory, incident_id)
    ids = await build_case(
        session_factory,
        incident_id,
        clock,
        kind=ReviewCaseKind.QUANTITY,
        delivered=Decimal("30"),
        allocation_status=AllocationStatus.CONFIRMED,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await login(client, auth, session_factory, "ops@example.org")
        response = await client.post(
            f"/operator/reviews/{ids['case_id']}/resolve",
            data={
                "csrf_token": csrf_for(token),
                "resolution": "REVISE_QUANTITY",
                "quantity": "20",
            },
        )
        assert response.status_code == 400
        assert "below delivered total" in response.text
    async with session_factory() as session:
        need = await session.get(Need, ids["need_id"])
        case = await session.get(ReviewCase, ids["case_id"])
    assert need is not None and need.quantity == Decimal("100.000")
    assert case is not None and case.status is ReviewStatus.OPEN


async def test_duplicate_resolution_preserves_corroboration(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    await add_operator(session_factory, incident_id)
    async with transaction(session_factory) as session:
        entity = Entity(
            incident_id=incident_id,
            name="Shelter 4",
            normalized_name="shelter 4",
            entity_type=EntityType.SHELTER,
        )
        session.add(entity)
        await session.flush()
        existing = Need(
            incident_id=incident_id,
            entity_id=entity.id,
            ref="RN-300001",
            resource=Resource.POTABLE_WATER,
            quantity=Decimal("120"),
            unit="l",
            quantity_source=QuantitySource.ESTIMATED,
            status=NeedStatus.PARTIAL,
        )
        session.add(existing)
        await session.flush()
        existing_id = existing.id
    ids = await build_case(
        session_factory,
        incident_id,
        clock,
        kind=ReviewCaseKind.DUPLICATE,
        need_quantity=Decimal("120"),
        payload={"existing_need_id": str(existing_id)},
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await login(client, auth, session_factory, "ops@example.org")
        response = await client.post(
            f"/operator/reviews/{ids['case_id']}/resolve",
            data={
                "csrf_token": csrf_for(token),
                "resolution": "MARK_DUPLICATE",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
    async with session_factory() as session:
        needs = list(await session.scalars(sa.select(Need).order_by(Need.ref)))
        case = await session.get(ReviewCase, ids["case_id"])
        event = await session.scalar(
            sa.select(EventLog).where(EventLog.event_type == "need.duplicate_marked")
        )
    assert len(needs) == 2
    assert {need.quantity for need in needs} == {Decimal("120.000")}
    assert case is not None and case.resolution == "MARK_DUPLICATE"
    assert event is not None


async def test_reject_on_a_need_level_medical_case_is_invalid(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    await add_operator(session_factory, incident_id)
    ids = await build_case(
        session_factory,
        incident_id,
        clock,
        kind=ReviewCaseKind.MEDICAL,
        needs_human_review=True,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await login(client, auth, session_factory, "ops@example.org")
        response = await client.post(
            f"/operator/reviews/{ids['case_id']}/resolve",
            data={"csrf_token": csrf_for(token), "resolution": "REJECT"},
        )
        assert response.status_code == 400
        assert "not valid for a medical case" in response.text
    async with session_factory() as session:
        case = await session.get(ReviewCase, ids["case_id"])
        need = await session.get(Need, ids["need_id"])
    assert case is not None and case.status is ReviewStatus.OPEN
    assert need is not None and need.needs_human_review is True


async def test_duplicate_resolution_is_not_valid_for_a_medical_case(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    await add_operator(session_factory, incident_id)
    ids = await build_case(
        session_factory,
        incident_id,
        clock,
        kind=ReviewCaseKind.MEDICAL,
        needs_human_review=True,
        payload={"existing_need_id": str(UUID(int=1))},
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await login(client, auth, session_factory, "ops@example.org")
        response = await client.post(
            f"/operator/reviews/{ids['case_id']}/resolve",
            data={
                "csrf_token": csrf_for(token),
                "resolution": "MARK_DUPLICATE",
            },
        )
        assert response.status_code == 400
        assert "not valid for a medical case" in response.text
    async with session_factory() as session:
        case = await session.get(ReviewCase, ids["case_id"])
    assert case is not None and case.status is ReviewStatus.OPEN


async def test_assignment_rejects_a_pending_action_for_another_sender(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    await add_operator(session_factory, incident_id)
    async with transaction(session_factory) as session:
        entity = Entity(
            incident_id=incident_id,
            name="Depot W",
            normalized_name="depot w",
            entity_type=EntityType.DEPOT,
            lat=22.51,
            lon=88.31,
        )
        session.add(entity)
        await session.flush()
        named = Sender(incident_id=incident_id, address="named@example.org")
        other = Sender(incident_id=incident_id, address="other-depot@example.org")
        session.add_all([named, other])
        await session.flush()
        message = Message(
            incident_id=incident_id,
            sender_id=other.id,
            provider_message_id="msg-other-depot",
            mailbox_id="mb-1",
            subject="Stock",
            body_text="500 l",
            received_at=NOW,
        )
        session.add(message)
        await session.flush()
        payload = {
            "entity_id": str(entity.id),
            "resource": Resource.POTABLE_WATER.value,
            "quantity": "500",
            "unit": "l",
            "report_id": None,
        }
        pending = PendingAction(
            kind=PendingActionKind.UPDATE_SUPPLY,
            incident_id=incident_id,
            sender_id=other.id,
            source_message_id=message.id,
            entity_id=entity.id,
            payload_json=payload,
            payload_hash=payload_hash(payload),
            status=PendingActionStatus.PENDING,
        )
        session.add(pending)
        await session.flush()
        case = ReviewCase(
            incident_id=incident_id,
            kind=ReviewCaseKind.ROLE_ASSIGNMENT,
            status=ReviewStatus.OPEN,
            source_message_id=message.id,
            reason="depot authority needed",
            payload={
                "sender_id": str(named.id),
                "entity_id": str(entity.id),
                "pending_action_id": str(pending.id),
            },
        )
        session.add(case)
        await session.flush()
        case_id = case.id
        named_id = named.id

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await login(client, auth, session_factory, "ops@example.org")
        response = await client.post(
            f"/operator/reviews/{case_id}/resolve",
            data={
                "csrf_token": csrf_for(token),
                "resolution": "ASSIGN_DEPOT_COORDINATOR",
            },
        )
        assert response.status_code == 400
        assert "does not belong" in response.text
    async with session_factory() as session:
        role = await session.scalar(
            sa.select(ParticipantRole).where(
                ParticipantRole.sender_id == named_id,
                ParticipantRole.role == ParticipantRoleKind.DEPOT_COORDINATOR,
            )
        )
        case = await session.get(ReviewCase, case_id)
    assert role is None
    assert case is not None and case.status is ReviewStatus.OPEN


async def test_assignment_grants_depot_authority_and_reissues_the_action_link(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    await add_operator(session_factory, incident_id)
    async with transaction(session_factory) as session:
        entity = Entity(
            incident_id=incident_id,
            name="Depot Z",
            normalized_name="depot z",
            entity_type=EntityType.DEPOT,
            lat=22.51,
            lon=88.31,
        )
        session.add(entity)
        await session.flush()
        sender = Sender(incident_id=incident_id, address="depot-z@example.org")
        session.add(sender)
        await session.flush()
        message = Message(
            incident_id=incident_id,
            sender_id=sender.id,
            provider_message_id="msg-depot-z",
            mailbox_id="mb-1",
            subject="Stock",
            body_text="500 l",
            received_at=NOW,
        )
        session.add(message)
        await session.flush()
        payload = {
            "entity_id": str(entity.id),
            "resource": Resource.POTABLE_WATER.value,
            "quantity": "500",
            "unit": "l",
            "report_id": None,
        }
        pending = PendingAction(
            kind=PendingActionKind.UPDATE_SUPPLY,
            incident_id=incident_id,
            sender_id=sender.id,
            source_message_id=message.id,
            entity_id=entity.id,
            payload_json=payload,
            payload_hash=payload_hash(payload),
            status=PendingActionStatus.PENDING,
        )
        session.add(pending)
        await session.flush()
        case = ReviewCase(
            incident_id=incident_id,
            kind=ReviewCaseKind.ROLE_ASSIGNMENT,
            status=ReviewStatus.OPEN,
            source_message_id=message.id,
            need_id=None,
            reason="depot authority needed",
            payload={
                "sender_id": str(sender.id),
                "entity_id": str(entity.id),
                "pending_action_id": str(pending.id),
            },
        )
        session.add(case)
        await session.flush()
        case_id = case.id
        sender_id = sender.id
        entity_id = entity.id

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await login(client, auth, session_factory, "ops@example.org")
        response = await client.post(
            f"/operator/reviews/{case_id}/resolve",
            data={
                "csrf_token": csrf_for(token),
                "resolution": "ASSIGN_DEPOT_COORDINATOR",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
    async with session_factory() as session:
        role = await session.scalar(
            sa.select(ParticipantRole).where(
                ParticipantRole.sender_id == sender_id,
                ParticipantRole.entity_id == entity_id,
                ParticipantRole.role == ParticipantRoleKind.DEPOT_COORDINATOR,
            )
        )
        email = await session.scalar(
            sa.select(OutboundEmail).where(
                OutboundEmail.to_address == "depot-z@example.org",
                OutboundEmail.kind.like("action_request:%"),
            )
        )
        case = await session.get(ReviewCase, case_id)
    assert role is not None
    assert email is not None
    assert "/actions/" in email.body_text
    assert case is not None and case.status is ReviewStatus.RESOLVED


async def test_resolution_requires_csrf_and_writes_an_audit_event(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    await add_operator(session_factory, incident_id)
    ids = await build_case(
        session_factory, incident_id, clock, kind=ReviewCaseKind.MEDICAL
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await login(client, auth, session_factory, "ops@example.org")
        denied = await client.post(
            f"/operator/reviews/{ids['case_id']}/resolve",
            data={"resolution": "APPROVE_PROPOSAL"},
        )
        assert denied.status_code == 403
        async with session_factory() as session:
            case = await session.get(ReviewCase, ids["case_id"])
        assert case is not None and case.status is ReviewStatus.OPEN

        allowed = await client.post(
            f"/operator/reviews/{ids['case_id']}/resolve",
            data={
                "csrf_token": csrf_for(token),
                "resolution": "APPROVE_PROPOSAL",
            },
            follow_redirects=False,
        )
        assert allowed.status_code == 303
    async with session_factory() as session:
        event = await session.scalar(
            sa.select(EventLog).where(EventLog.event_type == "review.resolved")
        )
        case = await session.get(ReviewCase, ids["case_id"])
    assert event is not None
    assert event.actor_id is not None
    assert case is not None and case.status is ReviewStatus.RESOLVED
    assert case.resolved_by_sender_id is not None

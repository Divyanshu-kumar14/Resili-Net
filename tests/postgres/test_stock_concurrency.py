"""Task 22: stock invariants under real PostgreSQL concurrency.

SQLite serializes writers, so these races can only be proven against
PostgreSQL row locks. Two aligned tasks (an Event barrier, never a sleep)
drive the real service transactions concurrently. Legal serializable
outcomes are accepted; the invariants are not negotiable:

* stock is never over-reserved and availability never goes negative;
* a single-use grant is consumed exactly once;
* a delivery decrements on-hand exactly once;
* cancellation and delivery cannot both win.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.db import transaction
from resilinet.domain.matching import AllocationDraft
from resilinet.domain.quantities import QuantitySource
from resilinet.models import (
    Allocation,
    Entity,
    Incident,
    Message,
    Need,
    PendingAction,
    PendingActionKind,
    Report,
    Sender,
    Supply,
)
from resilinet.schemas import (
    AllocationStatus,
    EntityType,
    NeedStatus,
    ReportKind,
    Resource,
)
from resilinet.security import Principal
from resilinet.services.actions import ActionService, create_action_grant, payload_hash
from resilinet.services.allocations import AllocationService
from resilinet.services.notifications import NotificationService

NOW = datetime(2026, 11, 22, 9, 0, 0, tzinfo=UTC)


async def seed_incident(
    factory: async_sessionmaker[AsyncSession], *, on_hand: str = "100"
) -> dict[str, UUID]:
    async with transaction(factory) as session:
        incident = Incident(key=f"race-{uuid4().hex[:8]}", name="Race", simulation=True)
        session.add(incident)
        await session.flush()
        shelter = Entity(
            incident_id=incident.id,
            name="Shelter 4",
            normalized_name="shelter 4",
            entity_type=EntityType.SHELTER,
            lat=Decimal("22.572600"),
            lon=Decimal("88.363900"),
            geo_confidence="exact",
        )
        depot = Entity(
            incident_id=incident.id,
            name="Depot B",
            normalized_name="depot b",
            entity_type=EntityType.DEPOT,
            lat=Decimal("22.572600"),
            lon=Decimal("88.363900"),
            geo_confidence="exact",
        )
        session.add_all([shelter, depot])
        await session.flush()
        supply = Supply(
            incident_id=incident.id,
            entity_id=depot.id,
            resource=Resource.POTABLE_WATER,
            on_hand=Decimal(on_hand),
            unit="l",
        )
        session.add(supply)
        await session.flush()
        return {
            "incident": incident.id,
            "shelter": shelter.id,
            "depot": depot.id,
            "supply": supply.id,
        }


async def make_need(
    factory: async_sessionmaker[AsyncSession],
    ids: dict[str, UUID],
    *,
    ref: str,
    quantity: str = "80",
) -> UUID:
    async with transaction(factory) as session:
        need = Need(
            incident_id=ids["incident"],
            entity_id=ids["shelter"],
            ref=ref,
            resource=Resource.POTABLE_WATER,
            quantity=Decimal(quantity),
            unit="l",
            quantity_source=QuantitySource.STATED,
            status=NeedStatus.OPEN,
        )
        session.add(need)
        await session.flush()
        return need.id


async def race(*coroutines) -> list:
    """Run coroutines aligned on one barrier; collect results/exceptions."""
    gate = asyncio.Event()

    async def wrapped(coro):
        await gate.wait()
        return await coro

    tasks = [asyncio.create_task(wrapped(c)) for c in coroutines]
    gate.set()
    return await asyncio.gather(*tasks, return_exceptions=True)


def successes(results: list) -> list:
    return [r for r in results if not isinstance(r, BaseException)]


def failures(results: list) -> list[BaseException]:
    return [r for r in results if isinstance(r, BaseException)]


async def stock_state(factory, ids) -> tuple[Decimal, Decimal, Decimal]:
    """(on_hand, active reserved, delivered) for the seeded supply."""
    async with factory() as session:
        supply = await session.get(Supply, ids["supply"])
        assert supply is not None
        allocations = list(
            await session.scalars(
                sa.select(Allocation).where(Allocation.supply_id == ids["supply"])
            )
        )
    reserved = sum(
        (a.quantity - a.delivered_quantity)
        for a in allocations
        if a.status in (AllocationStatus.PROPOSED, AllocationStatus.CONFIRMED)
    )
    delivered = sum((a.delivered_quantity for a in allocations), Decimal("0"))
    return supply.on_hand, reserved, delivered


async def test_two_workers_commit_against_one_supply(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await seed_incident(pg_factory, on_hand="100")
    need_a = await make_need(pg_factory, ids, ref="RN-100001")
    need_b = await make_need(pg_factory, ids, ref="RN-100002")
    service = AllocationService(clock=lambda: NOW)

    async def commit(need_id: UUID):
        async with transaction(pg_factory) as session:
            return await service.commit(
                session,
                need_id=need_id,
                drafts=[
                    AllocationDraft(
                        need_id=need_id,
                        supply_id=ids["supply"],
                        quantity=Decimal("80"),
                        distance_km=Decimal("1"),
                        score=Decimal("1"),
                    )
                ],
                critic_verdict="approved",
            )

    results = await race(commit(need_a), commit(need_b))
    assert len(successes(results)) >= 1
    on_hand, reserved, _ = await stock_state(pg_factory, ids)
    assert reserved <= on_hand, "stock over-reserved"
    assert on_hand - reserved >= 0, "negative availability"
    assert reserved == Decimal("100"), "capping must fill, not lose, the supply"


async def test_snapshot_races_reservation(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await seed_incident(pg_factory, on_hand="100")
    need_id = await make_need(pg_factory, ids, ref="RN-100003")
    service = AllocationService(clock=lambda: NOW)

    async def commit():
        async with transaction(pg_factory) as session:
            return await service.commit(
                session,
                need_id=need_id,
                drafts=[
                    AllocationDraft(
                        need_id=need_id,
                        supply_id=ids["supply"],
                        quantity=Decimal("80"),
                        distance_km=Decimal("1"),
                        score=Decimal("1"),
                    )
                ],
                critic_verdict="approved",
            )

    async def snapshot():
        async with transaction(pg_factory) as session:
            return await service.apply_supply_snapshot(
                session,
                incident_id=ids["incident"],
                entity_id=ids["depot"],
                resource=Resource.POTABLE_WATER,
                quantity=Decimal("50"),
                unit="l",
                source_report_id=None,
            )

    results = await race(commit(), snapshot())
    assert len(successes(results)) >= 1
    on_hand, reserved, delivered = await stock_state(pg_factory, ids)
    assert on_hand == Decimal("50")
    assert reserved <= on_hand, "reservation outlived the snapshot that shrank it"
    assert on_hand - reserved >= 0
    assert delivered == Decimal("0")


async def _confirmed_delivery_setup(
    factory: async_sessionmaker[AsyncSession], ids: dict[str, UUID]
) -> tuple[UUID, Principal]:
    """A confirmed allocation, its requester, and a live DELIVER grant."""
    async with transaction(factory) as session:
        requester = Sender(
            incident_id=ids["incident"], address="requester@example.org", verified=True
        )
        session.add(requester)
        await session.flush()
        message = Message(
            incident_id=ids["incident"],
            provider_message_id=f"race-{uuid4().hex[:8]}",
            mailbox_id="race-mailbox",
            thread_id="race-thread",
            sender_id=requester.id,
            subject="water",
            body_text="water please",
            status="processed",
            received_at=NOW,
        )
        session.add(message)
        await session.flush()
        report = Report(
            incident_id=ids["incident"],
            message_id=message.id,
            sender_id=requester.id,
            kind=ReportKind.NEED,
            resource=Resource.POTABLE_WATER,
            quantity=Decimal("40"),
            unit="l",
            raw_text="water please",
        )
        session.add(report)
        await session.flush()
        need = Need(
            incident_id=ids["incident"],
            entity_id=ids["shelter"],
            ref="RN-100004",
            resource=Resource.POTABLE_WATER,
            quantity=Decimal("40"),
            unit="l",
            quantity_source=QuantitySource.STATED,
            status=NeedStatus.MATCHED,
            source_report_id=report.id,
        )
        session.add(need)
        await session.flush()
        allocation = Allocation(
            incident_id=ids["incident"],
            need_id=need.id,
            supply_id=ids["supply"],
            ref="AL-100004",
            resource=Resource.POTABLE_WATER,
            quantity=Decimal("40"),
            unit="l",
            distance_km=Decimal("1"),
            status=AllocationStatus.CONFIRMED,
            critic_verdict="approved",
        )
        session.add(allocation)
        await session.flush()
        payload = {"quantity": "40"}
        pending = PendingAction(
            kind=PendingActionKind.DELIVER_ALLOCATION,
            incident_id=ids["incident"],
            sender_id=requester.id,
            source_message_id=message.id,
            source_report_id=report.id,
            entity_id=ids["shelter"],
            need_id=need.id,
            allocation_id=allocation.id,
            payload_json=payload,
            payload_hash=payload_hash(payload),
        )
        session.add(pending)
        await session.flush()
        grant, _token = await create_action_grant(
            session,
            kind=PendingActionKind.DELIVER_ALLOCATION,
            incident_id=ids["incident"],
            sender_id=requester.id,
            entity_id=ids["shelter"],
            need_id=need.id,
            allocation_id=allocation.id,
            pending_action_id=pending.id,
            payload_hash=pending.payload_hash,
            now=NOW,
        )
        principal = Principal(
            session_id=uuid4(),
            incident_id=ids["incident"],
            sender_id=requester.id,
            address=requester.address,
            expires_at=NOW + timedelta(hours=1),
            csrf_token="race-csrf",
        )
        return grant.id, principal


async def test_same_grant_delivery_posted_twice_consumes_once(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await seed_incident(pg_factory, on_hand="100")
    grant_id, principal = await _confirmed_delivery_setup(pg_factory, ids)
    service = ActionService(
        AllocationService(clock=lambda: NOW), NotificationService(), clock=lambda: NOW
    )

    async def post_delivery():
        async with transaction(pg_factory) as session:
            return await service.execute(session, grant_id=grant_id, principal=principal)

    results = await race(post_delivery(), post_delivery())
    assert len(successes(results)) == 1, results
    assert len(failures(results)) == 1, results

    on_hand, reserved, delivered = await stock_state(pg_factory, ids)
    assert delivered == Decimal("40"), "delivery must be recorded exactly once"
    assert on_hand == Decimal("60"), "on-hand must be decremented exactly once"
    assert reserved == Decimal("0"), "a delivered allocation no longer reserves"


async def test_cancellation_races_delivery(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await seed_incident(pg_factory, on_hand="100")
    need_id = await make_need(pg_factory, ids, ref="RN-100005", quantity="40")
    service = AllocationService(clock=lambda: NOW)
    async with transaction(pg_factory) as session:
        allocation = Allocation(
            incident_id=ids["incident"],
            need_id=need_id,
            supply_id=ids["supply"],
            ref="AL-100005",
            resource=Resource.POTABLE_WATER,
            quantity=Decimal("40"),
            unit="l",
            distance_km=Decimal("1"),
            status=AllocationStatus.CONFIRMED,
            critic_verdict="approved",
        )
        session.add(allocation)
        await session.flush()
        allocation_id = allocation.id

    async def cancel():
        async with transaction(pg_factory) as session:
            return await service.cancel_allocation(session, allocation_id=allocation_id)

    async def deliver():
        async with transaction(pg_factory) as session:
            return await service.record_delivery(
                session, allocation_id=allocation_id, quantity=Decimal("40")
            )

    results = await race(cancel(), deliver())
    assert len(successes(results)) == 1, results
    assert len(failures(results)) == 1, results

    async with pg_factory() as session:
        final = await session.get(Allocation, allocation_id)
    assert final is not None
    on_hand, _, delivered = await stock_state(pg_factory, ids)
    if final.status is AllocationStatus.CANCELLED:
        assert final.delivered_quantity == Decimal("0")
        assert on_hand == Decimal("100")
        assert delivered == Decimal("0")
    else:
        assert final.status is AllocationStatus.DELIVERED
        assert final.delivered_quantity == Decimal("40")
        assert on_hand == Decimal("60")
        assert delivered == Decimal("40")

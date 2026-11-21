from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.db import transaction
from resilinet.domain.lifecycle import remaining_need
from resilinet.domain.quantities import QuantitySource
from resilinet.models import Allocation, Entity, EventLog, Need, Sender, Supply
from resilinet.schemas import (
    AllocationStatus,
    EntityType,
    GeoConfidence,
    NeedStatus,
    Resource,
    Urgency,
)
from resilinet.services.allocations import AllocationService, AvailabilityError

from .conftest import Clock

LAT = Decimal("22.572600")
LON = Decimal("88.363900")


async def add_entity(
    session: AsyncSession,
    incident_id: UUID,
    name: str,
    entity_type: EntityType,
    *,
    lat: Decimal | None = LAT,
    lon: Decimal | None = LON,
) -> Entity:
    entity = Entity(
        incident_id=incident_id,
        name=name,
        normalized_name=name.casefold(),
        entity_type=entity_type,
        location_text=name,
        lat=lat,
        lon=lon,
        geo_confidence=GeoConfidence.EXACT if lat is not None else GeoConfidence.NONE,
        location_source="test",
    )
    session.add(entity)
    await session.flush()
    return entity


async def add_sender(
    session: AsyncSession, incident_id: UUID, address: str, *, verified: bool = True
) -> Sender:
    sender = Sender(incident_id=incident_id, address=address, verified=verified)
    session.add(sender)
    await session.flush()
    return sender


def service(clock: Clock) -> AllocationService:
    return AllocationService(clock=clock)


async def make_need(
    session: AsyncSession,
    svc: AllocationService,
    incident_id: UUID,
    entity: Entity,
    quantity: str,
) -> Need:
    return await svc.create_need(
        session,
        incident_id=incident_id,
        entity_id=entity.id,
        resource=Resource.POTABLE_WATER,
        quantity=Decimal(quantity),
        unit="l",
        quantity_source=QuantitySource.STATED,
        headcount=None,
        urgency=Urgency.HIGH,
        verified=True,
    )


async def make_supply(
    session: AsyncSession,
    svc: AllocationService,
    incident_id: UUID,
    entity: Entity,
    quantity: str,
) -> Supply:
    result = await svc.apply_supply_snapshot(
        session,
        incident_id=incident_id,
        entity_id=entity.id,
        resource=Resource.POTABLE_WATER,
        quantity=Decimal(quantity),
        unit="l",
        source_report_id=None,
    )
    return result.supply


async def propose(
    session: AsyncSession, svc: AllocationService, need: Need, clock: Clock
) -> tuple[Allocation, ...]:
    drafts = await svc.drafts_for_need(session, need.id, clock())
    result = await svc.commit(
        session, need_id=need.id, drafts=drafts, critic_verdict="approved"
    )
    return result.allocations


async def test_full_reservation_matches_the_need(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = service(clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "80")
        supply = await make_supply(session, svc, incident_id, depot, "100")

        allocations = await propose(session, svc, need, clock)

        assert len(allocations) == 1
        assert allocations[0].quantity == Decimal("80.000")
        assert allocations[0].status is AllocationStatus.PROPOSED
        assert await svc.available(session, supply) == Decimal("20.000")
        refreshed = await session.get(Need, need.id)
        assert refreshed is not None and refreshed.status is NeedStatus.MATCHED


async def test_partial_reservation_leaves_the_need_partial(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = service(clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "100")
        supply = await make_supply(session, svc, incident_id, depot, "30")

        allocations = await propose(session, svc, need, clock)

        assert allocations[0].quantity == Decimal("30.000")
        assert await svc.available(session, supply) == Decimal("0.000")
        refreshed = await session.get(Need, need.id)
        assert refreshed is not None and refreshed.status is NeedStatus.PARTIAL


async def test_split_reservation_across_two_supplies(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = service(clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot_a = await add_entity(session, incident_id, "Depot A", EntityType.DEPOT)
        depot_b = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "100")
        await make_supply(session, svc, incident_id, depot_a, "60")
        await make_supply(session, svc, incident_id, depot_b, "60")

        allocations = await propose(session, svc, need, clock)

        assert sum(a.quantity for a in allocations) == Decimal("100.000")
        assert len(allocations) == 2


async def test_stale_draft_is_capped_by_fresh_availability(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = service(clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        other = await add_entity(session, incident_id, "Shelter 9", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        first = await make_need(session, svc, incident_id, shelter, "80")
        second = await make_need(session, svc, incident_id, other, "70")
        supply = await make_supply(session, svc, incident_id, depot, "100")

        stale_drafts = await svc.drafts_for_need(session, first.id, clock())
        await propose(session, svc, second, clock)  # reserves 70 first
        result = await svc.commit(
            session, need_id=first.id, drafts=stale_drafts, critic_verdict="approved"
        )

        assert sum(a.quantity for a in result.allocations) == Decimal("30.000")
        assert await svc.available(session, supply) == Decimal("0.000")
        # The 80 -> 30 cap is auditable, not silent.
        adjusted = await session.scalar(
            sa.select(EventLog).where(EventLog.event_type == "allocation.adjusted")
        )
        assert adjusted is not None
        assert adjusted.payload["approved_quantity"] == "80.000"
        assert adjusted.payload["committed_quantity"] == "30.000"


async def test_committing_twice_does_not_double_reserve(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = service(clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "80")
        supply = await make_supply(session, svc, incident_id, depot, "100")

        await propose(session, svc, need, clock)
        again = await propose(session, svc, need, clock)

        assert again == ()
        assert await svc.available(session, supply) == Decimal("20.000")


async def test_cancellation_releases_the_reservation(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = service(clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "80")
        supply = await make_supply(session, svc, incident_id, depot, "100")
        (allocation,) = await propose(session, svc, need, clock)

        await svc.cancel_allocation(session, allocation_id=allocation.id)

        assert await svc.available(session, supply) == Decimal("100.000")
        refreshed = await session.get(Need, need.id)
        assert refreshed is not None and refreshed.status is NeedStatus.OPEN


async def test_snapshot_is_absolute_not_incremental(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = service(clock)
    async with transaction(session_factory) as session:
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)

        await make_supply(session, svc, incident_id, depot, "100")
        result = await svc.apply_supply_snapshot(
            session,
            incident_id=incident_id,
            entity_id=depot.id,
            resource=Resource.POTABLE_WATER,
            quantity=Decimal("150"),
            unit="l",
            source_report_id=None,
        )

        assert result.supply.on_hand == Decimal("150.000")


async def test_increased_need_fills_only_the_residual(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = service(clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "100")
        supply = await make_supply(session, svc, incident_id, depot, "200")
        await propose(session, svc, need, clock)

        await svc.update_need(session, need_id=need.id, new_quantity=Decimal("150"))
        added = await propose(session, svc, need, clock)

        assert sum(a.quantity for a in added) == Decimal("50.000")
        assert await svc.available(session, supply) == Decimal("50.000")


async def test_decreased_need_cancels_only_the_newest_excess(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = service(clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot_a = await add_entity(session, incident_id, "Depot A", EntityType.DEPOT)
        depot_b = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "100")
        supply_a = await make_supply(session, svc, incident_id, depot_a, "60")
        supply_b = await make_supply(session, svc, incident_id, depot_b, "40")
        allocations = await propose(session, svc, need, clock)
        assert len(allocations) == 2

        await svc.update_need(session, need_id=need.id, new_quantity=Decimal("70"))

        statuses = {
            a.id: a.status
            for a in await svc.allocations_for_need(session, need.id)
        }
        cancelled = [a for a in allocations if statuses[a.id] is AllocationStatus.CANCELLED]
        active = [a for a in allocations if statuses[a.id] is AllocationStatus.PROPOSED]
        assert len(cancelled) == 1 and len(active) == 1
        # The cancelled allocation is the newest one (drafted from Depot B last).
        assert cancelled[0].supply_id == supply_b.id
        assert active[0].supply_id == supply_a.id


async def test_lowered_snapshot_cancels_only_the_necessary_allocations(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = service(clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        other = await add_entity(session, incident_id, "Shelter 9", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        older_need = await make_need(session, svc, incident_id, shelter, "60")
        newer_need = await make_need(session, svc, incident_id, other, "40")
        supply = await make_supply(session, svc, incident_id, depot, "100")
        (older_alloc,) = await propose(session, svc, older_need, clock)
        clock.advance(hours=1)
        (newer_alloc,) = await propose(session, svc, newer_need, clock)

        result = await svc.apply_supply_snapshot(
            session,
            incident_id=incident_id,
            entity_id=depot.id,
            resource=Resource.POTABLE_WATER,
            quantity=Decimal("70"),
            unit="l",
            source_report_id=None,
        )

        assert [a.id for a in result.cancelled] == [newer_alloc.id]
        refreshed_older = await session.get(Allocation, older_alloc.id)
        assert refreshed_older is not None
        assert refreshed_older.status is AllocationStatus.PROPOSED
        assert await svc.available(session, supply) == Decimal("10.000")


async def test_partial_then_full_delivery_consumes_on_hand_once(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = service(clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "100")
        supply = await make_supply(session, svc, incident_id, depot, "100")
        (allocation,) = await propose(session, svc, need, clock)
        await svc.confirm_allocation(session, allocation_id=allocation.id)

        partial = await svc.record_delivery(
            session, allocation_id=allocation.id, quantity=Decimal("30")
        )
        assert partial.status is AllocationStatus.CONFIRMED
        assert partial.delivered_quantity == Decimal("30.000")
        refreshed_supply = await session.get(Supply, supply.id)
        assert refreshed_supply is not None and refreshed_supply.on_hand == Decimal("70.000")
        refreshed_need = await session.get(Need, need.id)
        assert refreshed_need is not None and refreshed_need.status is NeedStatus.PARTIAL

        done = await svc.record_delivery(
            session, allocation_id=allocation.id, quantity=Decimal("70")
        )
        assert done.status is AllocationStatus.DELIVERED
        refreshed_need = await session.get(Need, need.id)
        assert refreshed_need is not None and refreshed_need.status is NeedStatus.FULFILLED


async def test_cancellation_after_partial_delivery_keeps_delivered_history(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = service(clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "100")
        await make_supply(session, svc, incident_id, depot, "100")
        (allocation,) = await propose(session, svc, need, clock)
        await svc.confirm_allocation(session, allocation_id=allocation.id)
        await svc.record_delivery(
            session, allocation_id=allocation.id, quantity=Decimal("30")
        )

        await svc.cancel_allocation(session, allocation_id=allocation.id)

        allocations = await svc.allocations_for_need(session, need.id)
        assert remaining_need(Decimal("100"), allocations) == Decimal("70")


async def test_fulfilled_need_increased_leaves_exactly_the_new_residual(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = service(clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "100")
        await make_supply(session, svc, incident_id, depot, "200")
        (allocation,) = await propose(session, svc, need, clock)
        await svc.confirm_allocation(session, allocation_id=allocation.id)
        await svc.record_delivery(
            session, allocation_id=allocation.id, quantity=Decimal("100")
        )

        await svc.update_need(session, need_id=need.id, new_quantity=Decimal("150"))

        allocations = await svc.allocations_for_need(session, need.id)
        assert remaining_need(Decimal("150"), allocations) == Decimal("50")


async def test_need_revision_below_delivered_total_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = service(clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "100")
        await make_supply(session, svc, incident_id, depot, "100")
        (allocation,) = await propose(session, svc, need, clock)
        await svc.confirm_allocation(session, allocation_id=allocation.id)
        await svc.record_delivery(
            session, allocation_id=allocation.id, quantity=Decimal("30")
        )

        with pytest.raises(AvailabilityError):
            await svc.update_need(session, need_id=need.id, new_quantity=Decimal("20"))

        refreshed = await session.get(Need, need.id)
        assert refreshed is not None and refreshed.quantity == Decimal("100.000")


async def test_snapshot_cannot_redenominate_reserved_stock_into_another_unit(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = service(clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await svc.create_need(
            session,
            incident_id=incident_id,
            entity_id=shelter.id,
            resource=Resource.OTHER,
            quantity=Decimal("100"),
            unit="boxes",
            quantity_source=QuantitySource.STATED,
            headcount=None,
            urgency=Urgency.HIGH,
            verified=True,
        )
        snapshot = await svc.apply_supply_snapshot(
            session,
            incident_id=incident_id,
            entity_id=depot.id,
            resource=Resource.OTHER,
            quantity=Decimal("100"),
            unit="boxes",
            source_report_id=None,
        )
        supply = snapshot.supply
        drafts = await svc.drafts_for_need(session, need.id, clock())
        await svc.commit(
            session, need_id=need.id, drafts=drafts, critic_verdict="approved"
        )
        assert await svc.available(session, supply) == Decimal("0.000")

        with pytest.raises(AvailabilityError):
            await svc.apply_supply_snapshot(
                session,
                incident_id=incident_id,
                entity_id=depot.id,
                resource=Resource.OTHER,
                quantity=Decimal("80"),
                unit="cartons",
                source_report_id=None,
            )

        refreshed = await session.get(Supply, supply.id)
        assert refreshed is not None and refreshed.unit == "boxes"
        assert refreshed.on_hand == Decimal("100.000")


async def test_sub_resolution_quantities_are_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    from resilinet.services.allocations import AllocationStateError

    svc = service(clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        with pytest.raises(AllocationStateError):
            await svc.create_need(
                session,
                incident_id=incident_id,
                entity_id=shelter.id,
                resource=Resource.POTABLE_WATER,
                quantity=Decimal("0.0004"),
                unit="l",
                quantity_source=QuantitySource.STATED,
                headcount=None,
                urgency=Urgency.HIGH,
                verified=True,
            )
        need = await make_need(session, svc, incident_id, shelter, "100")
        await make_supply(session, svc, incident_id, depot, "100")
        (allocation,) = await propose(session, svc, need, clock)
        await svc.confirm_allocation(session, allocation_id=allocation.id)

        with pytest.raises(AllocationStateError):
            await svc.record_delivery(
                session, allocation_id=allocation.id, quantity=Decimal("0.0004")
            )
        refreshed = await session.get(Allocation, allocation.id)
        assert refreshed is not None
        assert refreshed.delivered_quantity == Decimal("0.000")


async def test_commit_after_cancellation_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    from resilinet.services.allocations import AllocationStateError

    svc = service(clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "80")
        supply = await make_supply(session, svc, incident_id, depot, "100")
        drafts = await svc.drafts_for_need(session, need.id, clock())

        await svc.cancel_need(session, need_id=need.id)

        with pytest.raises(AllocationStateError):
            await svc.commit(
                session, need_id=need.id, drafts=drafts, critic_verdict="approved"
            )
        assert await svc.available(session, supply) == Decimal("100.000")
        assert await svc.allocations_for_need(session, need.id) == []


async def test_commit_for_a_human_review_need_reserves_nothing(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = service(clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await svc.create_need(
            session,
            incident_id=incident_id,
            entity_id=shelter.id,
            resource=Resource.POTABLE_WATER,
            quantity=Decimal("80"),
            unit="l",
            quantity_source=QuantitySource.STATED,
            headcount=None,
            urgency=Urgency.HIGH,
            verified=True,
            needs_human_review=True,
        )
        supply = await make_supply(session, svc, incident_id, depot, "100")
        drafts = await svc.drafts_for_need(session, need.id, clock())

        result = await svc.commit(
            session, need_id=need.id, drafts=drafts, critic_verdict="approved"
        )

        assert result.allocations == ()
        assert await svc.available(session, supply) == Decimal("100.000")


async def test_lifecycle_events_are_recorded_without_pii(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = service(clock)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "50")
        await make_supply(session, svc, incident_id, depot, "50")
        (allocation,) = await propose(session, svc, need, clock)
        await svc.confirm_allocation(session, allocation_id=allocation.id)
        await svc.record_delivery(
            session, allocation_id=allocation.id, quantity=Decimal("50")
        )

        event_types = list(
            await session.scalars(
                sa.select(EventLog.event_type).where(EventLog.incident_id == incident_id)
            )
        )

        assert "need.created" in event_types
        assert "allocation.proposed" in event_types
        assert "allocation.confirmed" in event_types
        assert "allocation.delivered" in event_types

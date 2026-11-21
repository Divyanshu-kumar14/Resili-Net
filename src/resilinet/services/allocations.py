"""Allocation ledger for the Phase 0 vertical slice.

The durable implementation (Task 10) replaces the dictionaries with locked
database rows. The invariants encoded here are the ones that implementation
must preserve: available stock is never negative, reservations are active
while proposed or confirmed, and a need is never persisted below its delivered
total.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from ..domain.matching import (
    MAX_RADIUS_KM,
    freshness_factor,
    haversine_km,
    score_candidate,
)
from ..domain.quantities import CanonicalUnit, Quantity, QuantitySource
from ..domain.references import ReferenceSequence
from ..schemas import (
    AllocationStatus,
    EntityType,
    GeoConfidence,
    NeedStatus,
    Resource,
    Urgency,
)

ACTIVE_ALLOCATION_STATUSES = frozenset(
    {AllocationStatus.PROPOSED, AllocationStatus.CONFIRMED}
)


class AvailabilityError(ValueError):
    """An operation would make a stock invariant negative."""


@dataclass(frozen=True, slots=True)
class EntityRecord:
    id: UUID
    name: str
    entity_type: EntityType
    lat: Decimal | None
    lon: Decimal | None
    geo_confidence: GeoConfidence
    location_text: str | None


@dataclass(frozen=True, slots=True)
class NeedRecord:
    id: UUID
    ref: str
    entity_id: UUID
    resource: Resource
    quantity: Decimal
    unit: CanonicalUnit
    quantity_source: QuantitySource
    headcount: int | None
    urgency: Urgency
    status: NeedStatus
    verified: bool
    needs_human_review: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SupplyRecord:
    id: UUID
    entity_id: UUID
    resource: Resource
    on_hand: Decimal
    unit: CanonicalUnit
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AllocationRecord:
    id: UUID
    ref: str
    need_id: UUID
    supply_id: UUID
    resource: Resource
    quantity: Decimal
    unit: CanonicalUnit
    delivered_quantity: Decimal
    distance_km: Decimal
    status: AllocationStatus
    critic_verdict: str
    created_at: datetime
    updated_at: datetime


class AllocationService:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        references: ReferenceSequence | None = None,
        max_radius_km: Decimal = MAX_RADIUS_KM,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._references = references or ReferenceSequence()
        self._max_radius_km = max_radius_km
        self._entities: dict[UUID, EntityRecord] = {}
        self._entity_by_name: dict[str, UUID] = {}
        self._needs: dict[UUID, NeedRecord] = {}
        self._supplies: dict[UUID, SupplyRecord] = {}
        self._supply_by_key: dict[tuple[UUID, Resource], UUID] = {}
        self._allocations: dict[UUID, AllocationRecord] = {}

    # -- entities ---------------------------------------------------------

    def upsert_entity(
        self,
        *,
        name: str,
        entity_type: EntityType,
        lat: Decimal | None,
        lon: Decimal | None,
        geo_confidence: GeoConfidence,
        location_text: str | None,
    ) -> EntityRecord:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("entity name is required")
        key = clean_name.casefold()
        existing_id = self._entity_by_name.get(key)
        if existing_id is not None:
            existing = self._entities[existing_id]
            # An email may fill in a missing location, but it must never move or
            # re-type an existing pin. Conflicting reports are ignored here;
            # Task 9's duplicate/reconciliation rules own review escalation.
            updated = replace(
                existing,
                lat=existing.lat if existing.lat is not None else lat,
                lon=existing.lon if existing.lon is not None else lon,
                geo_confidence=(
                    existing.geo_confidence
                    if existing.geo_confidence is not GeoConfidence.NONE
                    else geo_confidence
                ),
                location_text=existing.location_text or location_text,
            )
            self._entities[existing_id] = updated
            return updated
        entity = EntityRecord(
            id=uuid4(),
            name=clean_name,
            entity_type=entity_type,
            lat=lat,
            lon=lon,
            geo_confidence=geo_confidence,
            location_text=location_text,
        )
        self._entities[entity.id] = entity
        self._entity_by_name[key] = entity.id
        return entity

    def entity(self, entity_id: UUID) -> EntityRecord:
        return self._entities[entity_id]

    # -- supplies ---------------------------------------------------------

    def check_snapshot(
        self,
        *,
        entity_id: UUID,
        resource: Resource,
        amount: Decimal,
    ) -> None:
        """Reject a snapshot that cannot support existing reservations.

        Validation is separated from the write so a compound message can be
        checked in full before any row changes.
        """
        existing_id = self._supply_by_key.get((entity_id, resource))
        if existing_id is None:
            return
        reserved = sum(
            (
                allocation.quantity - allocation.delivered_quantity
                for allocation in self._allocations.values()
                if allocation.supply_id == existing_id
                and allocation.status in ACTIVE_ALLOCATION_STATUSES
            ),
            Decimal("0"),
        )
        if amount < reserved:
            raise AvailabilityError(
                f"snapshot of {amount} cannot support {reserved} reserved"
            )

    def upsert_supply(
        self,
        *,
        entity_id: UUID,
        resource: Resource,
        quantity: Quantity,
        now: datetime | None = None,
    ) -> SupplyRecord:
        """Store an absolute on-hand snapshot for (entity, resource)."""
        supplied_at = now or self._clock()
        key = (entity_id, resource)
        existing_id = self._supply_by_key.get(key)
        self.check_snapshot(entity_id=entity_id, resource=resource, amount=quantity.amount)
        if existing_id is not None:
            updated = replace(
                self._supplies[existing_id],
                on_hand=quantity.amount,
                unit=quantity.unit,
                updated_at=supplied_at,
            )
            self._supplies[existing_id] = updated
            return updated
        supply = SupplyRecord(
            id=uuid4(),
            entity_id=entity_id,
            resource=resource,
            on_hand=quantity.amount,
            unit=quantity.unit,
            updated_at=supplied_at,
        )
        self._supplies[supply.id] = supply
        self._supply_by_key[key] = supply.id
        return supply

    def supply(self, supply_id: UUID) -> SupplyRecord:
        return self._supplies[supply_id]

    def available(self, supply_id: UUID) -> Decimal:
        supply = self._supplies[supply_id]
        reserved = sum(
            (
                allocation.quantity - allocation.delivered_quantity
                for allocation in self._allocations.values()
                if allocation.supply_id == supply_id
                and allocation.status in ACTIVE_ALLOCATION_STATUSES
            ),
            Decimal("0"),
        )
        available = supply.on_hand - reserved
        if available < 0:
            raise AvailabilityError(
                f"supply {supply.id} would have negative available stock"
            )
        return available

    # -- needs and proposals ---------------------------------------------

    def create_need(
        self,
        *,
        entity_id: UUID,
        resource: Resource,
        quantity: Quantity,
        headcount: int | None,
        urgency: Urgency,
        verified: bool,
        needs_human_review: bool,
        now: datetime | None = None,
    ) -> NeedRecord:
        if quantity.amount <= 0:
            raise ValueError("need quantity must be positive")
        created_at = now or self._clock()
        need = NeedRecord(
            id=uuid4(),
            ref=self._references.next("RN"),
            entity_id=entity_id,
            resource=resource,
            quantity=quantity.amount,
            unit=quantity.unit,
            quantity_source=quantity.source,
            headcount=headcount,
            urgency=urgency,
            status=NeedStatus.OPEN,
            verified=verified,
            needs_human_review=needs_human_review,
            created_at=created_at,
            updated_at=created_at,
        )
        self._needs[need.id] = need
        return need

    def need(self, need_id: UUID) -> NeedRecord:
        return self._needs[need_id]

    def propose(self, need_id: UUID) -> tuple[AllocationRecord, ...]:
        """Greedily reserve the residual need, then update the need status."""
        need = self._needs[need_id]
        need_entity = self._entities[need.entity_id]
        now = self._clock()

        delivered_total = sum(
            (
                allocation.delivered_quantity
                for allocation in self._allocations.values()
                if allocation.need_id == need_id
                and allocation.status is not AllocationStatus.REJECTED
            ),
            Decimal("0"),
        )
        reserved_remaining = sum(
            (
                allocation.quantity - allocation.delivered_quantity
                for allocation in self._allocations.values()
                if allocation.need_id == need_id
                and allocation.status in ACTIVE_ALLOCATION_STATUSES
            ),
            Decimal("0"),
        )
        remaining = need.quantity - delivered_total - reserved_remaining
        if remaining < 0:
            raise AvailabilityError(f"need {need.ref} is over-reserved")
        if remaining == 0:
            if delivered_total >= need.quantity:
                status = NeedStatus.FULFILLED
            elif delivered_total > 0:
                status = NeedStatus.PARTIAL
            elif reserved_remaining >= need.quantity:
                status = NeedStatus.MATCHED
            elif reserved_remaining > 0:
                status = NeedStatus.PARTIAL
            else:
                status = NeedStatus.OPEN
            self._needs[need.id] = replace(need, status=status, updated_at=now)
            return tuple(
                allocation
                for allocation in self._allocations.values()
                if allocation.need_id == need_id
                and allocation.status in ACTIVE_ALLOCATION_STATUSES
            )

        candidates: list[tuple[Decimal, Decimal, SupplyRecord, Decimal]] = []
        for supply in self._supplies.values():
            if supply.resource is not need.resource or supply.unit is not need.unit:
                continue
            available = self.available(supply.id)
            if available <= 0:
                continue
            supply_entity = self._entities[supply.entity_id]
            if (
                need_entity.lat is None
                or need_entity.lon is None
                or supply_entity.lat is None
                or supply_entity.lon is None
            ):
                continue
            distance = haversine_km(
                need_entity.lat,
                need_entity.lon,
                supply_entity.lat,
                supply_entity.lon,
            )
            if distance > self._max_radius_km:
                continue
            freshness = freshness_factor(supply.updated_at, now)
            score = score_candidate(
                distance_km=distance,
                available=available,
                remaining_need=remaining,
                freshness=freshness,
            )
            candidates.append((score, distance, supply, available))

        candidates.sort(key=lambda item: (-item[0], str(item[2].id)))
        created: list[AllocationRecord] = []
        for _, distance, supply, available in candidates:
            if remaining <= 0:
                break
            reserved = min(remaining, available)
            allocation = AllocationRecord(
                id=uuid4(),
                ref=self._references.next("AL"),
                need_id=need.id,
                supply_id=supply.id,
                resource=need.resource,
                quantity=reserved,
                unit=need.unit,
                delivered_quantity=Decimal("0"),
                distance_km=distance,
                status=AllocationStatus.PROPOSED,
                critic_verdict="pending",
                created_at=now,
                updated_at=now,
            )
            self._allocations[allocation.id] = allocation
            created.append(allocation)
            remaining -= reserved

        covered = need.quantity - remaining
        if delivered_total >= need.quantity:
            status = NeedStatus.FULFILLED
        elif delivered_total > 0:
            status = NeedStatus.PARTIAL
        elif covered >= need.quantity:
            status = NeedStatus.MATCHED
        elif covered > 0:
            status = NeedStatus.PARTIAL
        else:
            status = NeedStatus.OPEN
        self._needs[need.id] = replace(need, status=status, updated_at=now)
        return tuple(created)

    # -- read views -------------------------------------------------------

    def all_entities(self) -> Sequence[EntityRecord]:
        return tuple(self._entities.values())

    def all_needs(self) -> Sequence[NeedRecord]:
        return tuple(self._needs.values())

    def all_supplies(self) -> Sequence[SupplyRecord]:
        return tuple(self._supplies.values())

    def all_allocations(self) -> Sequence[AllocationRecord]:
        return tuple(self._allocations.values())

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from resilinet.domain.quantities import CanonicalUnit, Quantity, QuantitySource
from resilinet.domain.references import ReferenceSequence
from resilinet.schemas import EntityType, GeoConfidence, Resource, Urgency
from resilinet.services.allocations import AllocationService, AvailabilityError

NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)


def make_service() -> AllocationService:
    return AllocationService(clock=lambda: NOW, references=ReferenceSequence())


def add_entity(
    service: AllocationService,
    name: str,
    entity_type: EntityType,
    lat: str,
    lon: str,
):
    return service.upsert_entity(
        name=name,
        entity_type=entity_type,
        lat=Decimal(lat),
        lon=Decimal(lon),
        geo_confidence=GeoConfidence.EXACT,
        location_text=name,
    )


def stated(amount: str) -> Quantity:
    return Quantity(
        amount=Decimal(amount),
        unit=CanonicalUnit.LITRE,
        source=QuantitySource.STATED,
    )


def add_supply(service: AllocationService, entity_id, amount: str):
    return service.upsert_supply(
        entity_id=entity_id,
        resource=Resource.POTABLE_WATER,
        quantity=stated(amount),
        now=NOW,
    )


def add_need(service: AllocationService, entity_id, amount: str):
    return service.create_need(
        entity_id=entity_id,
        resource=Resource.POTABLE_WATER,
        quantity=stated(amount),
        headcount=None,
        urgency=Urgency.HIGH,
        verified=False,
        needs_human_review=False,
        now=NOW,
    )


def test_proposing_twice_does_not_double_reserve() -> None:
    service = make_service()
    shelter = add_entity(service, "Shelter 4", EntityType.SHELTER, "18.5204", "73.8567")
    depot = add_entity(service, "Depot B", EntityType.DEPOT, "18.5310", "73.8710")
    supply = add_supply(service, depot.id, "100")
    need = add_need(service, shelter.id, "80")

    first = service.propose(need.id)
    second = service.propose(need.id)

    assert len(first) == 1
    assert second == first
    assert service.available(supply.id) == Decimal("20.000")
    assert service.need(need.id).status.value == "matched"


def test_snapshot_below_active_reservations_is_rejected() -> None:
    service = make_service()
    shelter = add_entity(service, "Shelter 4", EntityType.SHELTER, "18.5204", "73.8567")
    depot = add_entity(service, "Depot B", EntityType.DEPOT, "18.5310", "73.8710")
    supply = add_supply(service, depot.id, "100")
    need = add_need(service, shelter.id, "80")
    service.propose(need.id)

    with pytest.raises(AvailabilityError):
        add_supply(service, depot.id, "50")

    assert service.supply(supply.id).on_hand == Decimal("100.000")
    assert service.available(supply.id) == Decimal("20.000")


def test_new_reports_cannot_relocate_or_retype_an_existing_entity() -> None:
    service = make_service()
    first = add_entity(service, "Shelter 4", EntityType.SHELTER, "18.5204", "73.8567")

    second = add_entity(service, "shelter 4", EntityType.DEPOT, "19.0000", "74.0000")

    assert second.id == first.id
    assert second.lat == Decimal("18.5204")
    assert second.lon == Decimal("73.8567")
    assert second.entity_type is EntityType.SHELTER
    assert second.location_text == "Shelter 4"

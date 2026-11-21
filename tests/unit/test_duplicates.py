from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from resilinet.domain.duplicates import (
    DuplicateKind,
    DuplicateNeedView,
    find_duplicate,
    name_similarity,
)
from resilinet.schemas import NeedStatus, Resource

NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
BASE_LAT = Decimal("22.572600")
BASE_LON = Decimal("88.363900")
# Roughly 100 m north of the base point.
NEAR_LAT = Decimal("22.573500")


def candidate(
    suffix: int,
    *,
    entity_id: UUID | None = None,
    name: str = "Shelter 4",
    resource: Resource = Resource.POTABLE_WATER,
    unit: str = "l",
    lat: Decimal | None = BASE_LAT,
    lon: Decimal | None = BASE_LON,
    status: NeedStatus = NeedStatus.OPEN,
    age_hours: float = 1,
) -> DuplicateNeedView:
    return DuplicateNeedView(
        need_id=UUID(int=suffix),
        entity_id=entity_id if entity_id is not None else UUID(int=1000 + suffix),
        entity_name=name,
        resource=resource,
        unit=unit,
        lat=lat,
        lon=lon,
        status=status,
        created_at=NOW - timedelta(hours=age_hours),
    )


def test_same_entity_and_resource_within_twelve_hours_is_a_duplicate() -> None:
    entity = UUID(int=42)
    incoming = candidate(1, entity_id=entity)
    existing = candidate(2, entity_id=entity, age_hours=3)

    decision = find_duplicate(incoming, [existing], NOW)

    assert decision.kind is DuplicateKind.DUPLICATE
    assert decision.existing_id == existing.need_id


def test_similar_name_with_nearby_coordinates_is_a_duplicate() -> None:
    incoming = candidate(1, name="Community Shelter Four")
    existing = candidate(2, name="Community Shelter 4", lat=NEAR_LAT, age_hours=2)

    decision = find_duplicate(incoming, [existing], NOW)

    assert decision.kind is DuplicateKind.DUPLICATE


def test_unicode_names_normalize_for_similarity() -> None:
    assert name_similarity("Café  Shelter", "café shelter") >= 0.85


def test_two_distinct_shelters_100m_apart_are_never_merged() -> None:
    incoming = candidate(1, name="Shelter 4")
    nearby = candidate(2, name="Community Hall", lat=NEAR_LAT, age_hours=1)

    decision = find_duplicate(incoming, [nearby], NOW)

    assert decision.kind is DuplicateKind.REVIEW_CANDIDATE
    assert decision.existing_id == nearby.need_id


def test_similar_name_without_coordinates_is_not_enough() -> None:
    incoming = candidate(1, name="Community Shelter Four", lat=None, lon=None)
    existing = candidate(2, name="Community Shelter 4", lat=None, lon=None)

    decision = find_duplicate(incoming, [existing], NOW)

    assert decision.kind is DuplicateKind.NONE


def test_different_unit_is_never_a_duplicate() -> None:
    entity = UUID(int=42)
    incoming = candidate(1, entity_id=entity, unit="boxes")
    existing = candidate(2, entity_id=entity, unit="cartons")

    assert find_duplicate(incoming, [existing], NOW).kind is DuplicateKind.NONE


def test_different_resource_is_never_a_duplicate() -> None:
    entity = UUID(int=42)
    incoming = candidate(1, entity_id=entity, resource=Resource.FOOD)
    existing = candidate(2, entity_id=entity, resource=Resource.POTABLE_WATER)

    assert find_duplicate(incoming, [existing], NOW).kind is DuplicateKind.NONE


def test_closed_or_fulfilled_needs_are_never_duplicates() -> None:
    entity = UUID(int=42)
    incoming = candidate(1, entity_id=entity)
    for status in (NeedStatus.FULFILLED, NeedStatus.CANCELLED):
        existing = candidate(2, entity_id=entity, status=status)
        assert find_duplicate(incoming, [existing], NOW).kind is DuplicateKind.NONE


def test_need_exactly_twelve_hours_old_is_outside_the_window() -> None:
    entity = UUID(int=42)
    incoming = candidate(1, entity_id=entity)
    existing = candidate(2, entity_id=entity, age_hours=12)

    assert find_duplicate(incoming, [existing], NOW).kind is DuplicateKind.NONE


def test_need_thirteen_hours_old_is_outside_the_window() -> None:
    entity = UUID(int=42)
    incoming = candidate(1, entity_id=entity)
    existing = candidate(2, entity_id=entity, age_hours=13)

    assert find_duplicate(incoming, [existing], NOW).kind is DuplicateKind.NONE

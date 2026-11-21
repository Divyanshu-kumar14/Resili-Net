from decimal import Decimal
from uuid import UUID

from resilinet.domain.matching import AllocationDraft
from resilinet.domain.rules import (
    DraftValidationContext,
    SupplyValidationView,
    is_medical_report,
    revise_drafts,
    validate_drafts,
)
from resilinet.schemas import GeoConfidence, Resource

NEED_ID = UUID(int=1)
SUPPLY_ID = UUID(int=2)
OTHER_SUPPLY_ID = UUID(int=3)
LAT = Decimal("22.572600")
LON = Decimal("88.363900")


def supply(
    supply_id: UUID = SUPPLY_ID,
    *,
    available: str = "100",
    resource: str = "potable_water",
    unit: str = "l",
    lat: Decimal | None = LAT,
    lon: Decimal | None = LON,
) -> SupplyValidationView:
    return SupplyValidationView(
        id=supply_id,
        resource=resource,
        unit=unit,
        available=Decimal(available),
        entity_lat=lat,
        entity_lon=lon,
    )


def context(
    *,
    remaining: str = "100",
    supplies: dict[UUID, SupplyValidationView] | None = None,
    need_lat: Decimal | None = LAT,
    need_lon: Decimal | None = LON,
) -> DraftValidationContext:
    return DraftValidationContext(
        need_id=NEED_ID,
        resource="potable_water",
        unit="l",
        remaining=Decimal(remaining),
        need_lat=need_lat,
        need_lon=need_lon,
        need_geo_confidence=GeoConfidence.EXACT,
        supplies=supplies if supplies is not None else {SUPPLY_ID: supply()},
    )


def draft(
    *,
    supply_id: UUID = SUPPLY_ID,
    quantity: str = "80",
    distance: str = "1.5",
) -> AllocationDraft:
    return AllocationDraft(
        need_id=NEED_ID,
        supply_id=supply_id,
        quantity=Decimal(quantity),
        distance_km=Decimal(distance),
        score=Decimal("0.9"),
    )


def codes(drafts: list[AllocationDraft], ctx: DraftValidationContext) -> set[str]:
    return {violation.code for violation in validate_drafts(drafts, ctx)}


def test_valid_draft_has_no_violations() -> None:
    assert validate_drafts([draft()], context()) == []


def test_over_allocation_beyond_remaining_need_is_flagged() -> None:
    assert "OVER_ALLOCATION" in codes([draft(quantity="150")], context())


def test_over_allocation_beyond_supply_availability_is_flagged() -> None:
    ctx = context(supplies={SUPPLY_ID: supply(available="50")})

    assert "OVER_ALLOCATION" in codes([draft(quantity="80")], ctx)


def test_candidate_beyond_fifty_km_is_flagged() -> None:
    assert "DISTANCE_EXCEEDED" in codes([draft(distance="60")], context())


def test_resource_and_unit_mismatches_are_flagged() -> None:
    ctx = context(
        supplies={SUPPLY_ID: supply(resource="food", unit="meal")},
    )

    found = codes([draft()], ctx)

    assert "RESOURCE_MISMATCH" in found
    assert "UNIT_MISMATCH" in found


def test_duplicate_candidate_for_one_supply_is_flagged() -> None:
    drafts = [draft(quantity="40"), draft(quantity="40")]

    assert "DUPLICATE_CANDIDATE" in codes(drafts, context())


def test_missing_coordinates_make_the_location_ambiguous() -> None:
    ctx = context(
        supplies={SUPPLY_ID: supply(lat=None, lon=None)},
    )

    assert "AMBIGUOUS_LOCATION" in codes([draft()], ctx)


def test_implausible_quantity_is_flagged_not_clamped() -> None:
    ctx = context(remaining="2000000", supplies={SUPPLY_ID: supply(available="2000000")})

    assert "IMPLAUSIBLE_QUANTITY" in codes([draft(quantity="1000001")], ctx)


def test_revision_caps_quantities_and_drops_unusable_candidates() -> None:
    ctx = context(
        supplies={
            SUPPLY_ID: supply(available="60"),
            OTHER_SUPPLY_ID: supply(resource="food", unit="meal"),
        }
    )
    drafts = [
        draft(quantity="150"),
        draft(supply_id=OTHER_SUPPLY_ID, quantity="10"),
    ]

    revised = revise_drafts(drafts, ctx)

    assert len(revised) == 1
    assert revised[0].supply_id == SUPPLY_ID
    assert revised[0].quantity == Decimal("60.000")
    assert validate_drafts(revised, ctx) == []


def test_medical_detection_covers_resources_and_terms() -> None:
    assert is_medical_report(Resource.MEDICINE, "two boxes")
    assert is_medical_report(Resource.FIRST_AID, "bandages")
    assert is_medical_report(Resource.OTHER, "need insulin for a patient")
    assert is_medical_report(Resource.POTABLE_WATER, "oxygen concentrator broken")
    assert is_medical_report(Resource.FOOD, "a pregnant volunteer needs rest")
    assert not is_medical_report(Resource.POTABLE_WATER, "40 people need drinking water")
    assert not is_medical_report(Resource.BLANKETS, "blankets for the night shelter")


def test_medical_detection_covers_inflected_and_clinical_language() -> None:
    assert is_medical_report(Resource.POTABLE_WATER, "injured people need water")
    assert is_medical_report(Resource.OTHER, "5 injuries reported at the hall")
    assert is_medical_report(Resource.OTHER, "need medication for two patients")
    assert is_medical_report(Resource.OTHER, "pregnancy clinic needs blankets")
    assert is_medical_report(Resource.OTHER, "asthma inhalers ran out")
    assert is_medical_report(Resource.FOOD, "one volunteer has a broken leg")
    assert not is_medical_report(Resource.FOOD, "40 meals for the night shelter")

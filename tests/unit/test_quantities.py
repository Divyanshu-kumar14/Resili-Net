from decimal import Decimal

import pytest

from resilinet.domain.quantities import QuantityError, estimate_need, to_canonical
from resilinet.schemas import Resource


def test_gallon_conversion_and_headcount_estimate() -> None:
    converted = to_canonical(Resource.POTABLE_WATER, Decimal("300"), "gallons")
    estimated = estimate_need(Resource.POTABLE_WATER, 40)

    assert converted.amount == Decimal("1135.500")
    assert converted.unit.value == "l"
    assert estimated is not None
    assert estimated.amount == Decimal("120.000")
    assert estimated.source.value == "estimated"


@pytest.mark.parametrize(
    "quantity",
    [
        Decimal("1e999999"),
        Decimal("0.0001"),
    ],
)
def test_unusable_quantities_raise_quantity_error(quantity: Decimal) -> None:
    with pytest.raises(QuantityError):
        to_canonical(Resource.POTABLE_WATER, quantity, "l")


def test_negative_and_unknown_units_are_rejected() -> None:
    with pytest.raises(QuantityError):
        to_canonical(Resource.POTABLE_WATER, Decimal("-1"), "l")
    with pytest.raises(QuantityError):
        to_canonical(Resource.POTABLE_WATER, Decimal("1"), "barrels")
    with pytest.raises(QuantityError):
        to_canonical(Resource.MEDICINE, Decimal("1"), "doses")


def test_water_above_the_safety_cap_is_rejected() -> None:
    with pytest.raises(QuantityError):
        to_canonical(Resource.POTABLE_WATER, Decimal("10001"), "l")
    with pytest.raises(QuantityError):
        to_canonical(Resource.POTABLE_WATER, Decimal("2644"), "gallons")


# -- Task 8: estimates, zero semantics, rounding, non-canonical resources -----


@pytest.mark.parametrize(
    ("resource", "expected"),
    [
        (Resource.POTABLE_WATER, Decimal("120.000")),
        (Resource.FOOD, Decimal("80.000")),
        (Resource.BLANKETS, Decimal("40.000")),
    ],
)
def test_one_day_planning_estimates_for_forty_people(
    resource: Resource, expected: Decimal
) -> None:
    estimated = estimate_need(resource, 40)

    assert estimated is not None
    assert estimated.amount == expected
    assert estimated.source.value == "estimated"


@pytest.mark.parametrize(
    "resource",
    [Resource.MEDICINE, Resource.SANITATION, Resource.TRANSPORT, Resource.OTHER],
)
def test_unsupported_resources_have_no_planning_estimate(resource: Resource) -> None:
    assert estimate_need(resource, 40) is None


def test_estimate_requires_a_positive_headcount() -> None:
    assert estimate_need(Resource.POTABLE_WATER, None) is None
    assert estimate_need(Resource.POTABLE_WATER, 0) is None


def test_zero_supply_snapshot_is_a_valid_empty_quantity() -> None:
    quantity = to_canonical(Resource.POTABLE_WATER, Decimal("0"), "l")

    assert quantity.amount == Decimal("0.000")


@pytest.mark.parametrize(
    "quantity",
    [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("1000001")],
)
def test_non_finite_and_implausible_quantities_are_rejected(quantity: Decimal) -> None:
    with pytest.raises(QuantityError):
        to_canonical(Resource.POTABLE_WATER, quantity, "l")


def test_canonical_amounts_round_half_even_to_millilitre_resolution() -> None:
    assert to_canonical(Resource.POTABLE_WATER, Decimal("1.2345"), "l").amount == Decimal(
        "1.234"
    )
    assert to_canonical(Resource.POTABLE_WATER, Decimal("2.5"), "gallons").amount == Decimal(
        "9.462"
    )


def test_over_cap_quantities_are_rejected_never_clamped() -> None:
    with pytest.raises(QuantityError):
        to_canonical(Resource.FOOD, Decimal("1000001"), "meals")


def test_canonical_unit_lookup_distinguishes_unmapped_resources() -> None:
    from resilinet.domain.quantities import canonical_unit

    assert canonical_unit(Resource.POTABLE_WATER).value == "l"  # type: ignore[union-attr]
    assert canonical_unit(Resource.FOOD).value == "meal"  # type: ignore[union-attr]
    assert canonical_unit(Resource.BLANKETS).value == "count"  # type: ignore[union-attr]
    assert canonical_unit(Resource.SANITATION) is None
    assert canonical_unit(Resource.MEDICINE) is None


def test_non_canonical_resource_keeps_its_stated_unit() -> None:
    from resilinet.domain.quantities import to_stated

    stated = to_stated(Resource.SANITATION, Decimal("50"), " Kits ")

    assert stated.amount == Decimal("50.000")
    assert stated.unit == "kits"


def test_non_canonical_resource_still_validates_amount_and_unit() -> None:
    from resilinet.domain.quantities import to_stated

    with pytest.raises(QuantityError):
        to_stated(Resource.SANITATION, Decimal("-1"), "kits")
    with pytest.raises(QuantityError):
        to_stated(Resource.SANITATION, Decimal("1000001"), "kits")
    with pytest.raises(QuantityError):
        to_stated(Resource.SANITATION, Decimal("1"), "  ")
    with pytest.raises(QuantityError):
        to_stated(Resource.SANITATION, None, "kits")

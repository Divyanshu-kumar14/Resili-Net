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

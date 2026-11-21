"""Canonical relief quantities and planning estimates.

Canonical units are litres (``l``) for water, meals (``meal``) for food, and
items (``count``) for blankets. Gallons convert with the PRD's explicit
``x 3.785``. Values are Decimal, finite, non-negative, and rounded to three
decimal places. Units are never guessed from names outside the explicit table.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from ..schemas import Resource

QUANTUM = Decimal("0.001")
GALLONS_TO_LITRES = Decimal("3.785")
MAX_ACCEPTED_LITRES = Decimal("10000")
MAX_ACCEPTED_AMOUNT = Decimal("1000000")


class CanonicalUnit(StrEnum):
    LITRE = "l"
    MEAL = "meal"
    COUNT = "count"


class QuantitySource(StrEnum):
    STATED = "stated"
    ESTIMATED = "estimated"


@dataclass(frozen=True, slots=True)
class Quantity:
    amount: Decimal
    unit: CanonicalUnit
    source: QuantitySource


class QuantityError(ValueError):
    """A quantity or unit cannot be accepted without clarification."""


_CANONICAL_UNIT: dict[Resource, CanonicalUnit] = {
    Resource.POTABLE_WATER: CanonicalUnit.LITRE,
    Resource.FOOD: CanonicalUnit.MEAL,
    Resource.BLANKETS: CanonicalUnit.COUNT,
}

_LITRE_UNITS = frozenset({"l", "litre", "litres", "liter", "liters"})
_GALLON_UNITS = frozenset({"gal", "gallon", "gallons"})
_MEAL_UNITS = frozenset({"meal", "meals"})
_COUNT_UNITS = frozenset({"count", "counts", "unit", "units", "blanket", "blankets"})

# One-day planning estimates. These are assumptions, always labelled estimated.
_PLANNING_RATES: dict[Resource, Decimal] = {
    Resource.POTABLE_WATER: Decimal("3"),
    Resource.FOOD: Decimal("2"),
    Resource.BLANKETS: Decimal("1"),
}


def _validate(amount: Decimal) -> Decimal:
    if not amount.is_finite():
        raise QuantityError("quantity must be finite")
    if amount < 0:
        raise QuantityError("quantity cannot be negative")
    if amount > MAX_ACCEPTED_AMOUNT:
        raise QuantityError("quantity exceeds the accepted safety cap")
    try:
        quantized = amount.quantize(QUANTUM)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise QuantityError("quantity is outside the accepted range") from exc
    if amount > 0 and quantized == 0:
        raise QuantityError("quantity is smaller than the canonical resolution")
    return quantized


def to_canonical(resource: Resource, quantity: Decimal | None, unit: str | None) -> Quantity:
    """Convert a stated quantity into the resource's canonical unit."""
    if quantity is None:
        raise QuantityError("quantity is missing")
    amount = _validate(quantity)
    canonical = _CANONICAL_UNIT.get(resource)
    if canonical is None:
        raise QuantityError(f"resource {resource.value} has no canonical unit yet")
    if unit is None or not unit.strip():
        raise QuantityError("unit is missing")

    normalized = unit.strip().casefold()
    if canonical is CanonicalUnit.LITRE:
        if normalized in _LITRE_UNITS:
            converted = amount
        elif normalized in _GALLON_UNITS:
            converted = amount * GALLONS_TO_LITRES
        else:
            raise QuantityError(f"unit {unit!r} is not a water unit")
        if converted > MAX_ACCEPTED_LITRES:
            raise QuantityError("water quantity exceeds the accepted safety cap")
    elif canonical is CanonicalUnit.MEAL:
        if normalized not in _MEAL_UNITS:
            raise QuantityError(f"unit {unit!r} is not a food unit")
        converted = amount
    else:
        if normalized not in _COUNT_UNITS:
            raise QuantityError(f"unit {unit!r} is not a blanket unit")
        converted = amount

    return Quantity(
        amount=converted.quantize(QUANTUM),
        unit=canonical,
        source=QuantitySource.STATED,
    )


def estimate_need(resource: Resource, headcount: int | None) -> Quantity | None:
    """Return a labelled one-day estimate, or ``None`` when not estimable."""
    rate = _PLANNING_RATES.get(resource)
    if rate is None or headcount is None or headcount <= 0:
        return None
    return Quantity(
        amount=(rate * headcount).quantize(QUANTUM),
        unit=_CANONICAL_UNIT[resource],
        source=QuantitySource.ESTIMATED,
    )


def format_quantity(quantity: Quantity) -> str:
    """Display a quantity without trailing zeros, prefixed by ``~`` if estimated."""
    value = f"{quantity.amount.normalize():f}"
    prefix = "~" if quantity.source is QuantitySource.ESTIMATED else ""
    return f"{prefix}{value} {quantity.unit.value}"

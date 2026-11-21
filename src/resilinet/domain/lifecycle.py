"""Allocation lifecycle and canonical stock arithmetic.

The formulas here are the contract every stock mutation must preserve:

* ``reserved_remaining`` counts only proposed/confirmed allocations;
* ``delivered_total`` counts every non-rejected allocation, including a
  partially delivered allocation that was later cancelled;
* ``remaining_need`` is never negative for a persisted need, and a need is
  never revised below its delivered total.

A proposal reserves stock but is not a dispatch; only confirmation commits
stock, and only an explicit delivery report consumes on-hand stock.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from ..schemas import AllocationStatus, NeedStatus

ACTIVE_ALLOCATION_STATUSES = frozenset(
    {AllocationStatus.PROPOSED, AllocationStatus.CONFIRMED}
)

_ALLOCATION_TRANSITIONS: dict[AllocationStatus, frozenset[AllocationStatus]] = {
    AllocationStatus.PROPOSED: frozenset(
        {
            AllocationStatus.CONFIRMED,
            AllocationStatus.CANCELLED,
            AllocationStatus.REJECTED,
        }
    ),
    AllocationStatus.CONFIRMED: frozenset(
        {AllocationStatus.DELIVERED, AllocationStatus.CANCELLED}
    ),
    AllocationStatus.DELIVERED: frozenset(),
    AllocationStatus.CANCELLED: frozenset(),
    AllocationStatus.REJECTED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class AllocationQuantities:
    """The quantity-bearing projection of one allocation."""

    quantity: Decimal
    delivered_quantity: Decimal
    status: AllocationStatus


def can_transition(current: AllocationStatus, target: AllocationStatus) -> bool:
    """Whether an allocation may move from ``current`` to ``target``."""
    return target in _ALLOCATION_TRANSITIONS[current]


def reserved_remaining(allocations: Iterable[AllocationQuantities]) -> Decimal:
    """Stock still held by active (proposed/confirmed) allocations."""
    return sum(
        (
            allocation.quantity - allocation.delivered_quantity
            for allocation in allocations
            if allocation.status in ACTIVE_ALLOCATION_STATUSES
        ),
        Decimal("0"),
    )


def delivered_total(allocations: Iterable[AllocationQuantities]) -> Decimal:
    """Everything ever delivered, excluding rejected allocations."""
    return sum(
        (
            allocation.delivered_quantity
            for allocation in allocations
            if allocation.status is not AllocationStatus.REJECTED
        ),
        Decimal("0"),
    )


def remaining_need(
    quantity: Decimal, allocations: Iterable[AllocationQuantities]
) -> Decimal:
    """Unmet quantity: required minus delivered minus actively reserved."""
    rows = list(allocations)
    return quantity - delivered_total(rows) - reserved_remaining(rows)


def derive_need_status(
    *,
    quantity: Decimal,
    delivered_total: Decimal,
    reserved_remaining: Decimal,
    cancelled: bool,
) -> NeedStatus:
    """The canonical need status for the given delivered/reserved totals."""
    if cancelled:
        return NeedStatus.CANCELLED
    if quantity > 0 and delivered_total >= quantity:
        return NeedStatus.FULFILLED
    if delivered_total > 0:
        return NeedStatus.PARTIAL
    if reserved_remaining <= 0:
        return NeedStatus.OPEN
    if reserved_remaining >= quantity:
        return NeedStatus.MATCHED
    return NeedStatus.PARTIAL

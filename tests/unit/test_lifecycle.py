from decimal import Decimal

import pytest

from resilinet.domain.lifecycle import (
    AllocationQuantities,
    can_transition,
    delivered_total,
    derive_need_status,
    remaining_need,
    reserved_remaining,
)
from resilinet.schemas import AllocationStatus, NeedStatus


def quantities(
    quantity: str, delivered: str = "0", status: AllocationStatus = AllocationStatus.PROPOSED
) -> AllocationQuantities:
    return AllocationQuantities(
        quantity=Decimal(quantity),
        delivered_quantity=Decimal(delivered),
        status=status,
    )


def test_reserved_remaining_counts_only_active_allocations() -> None:
    allocations = [
        quantities("100", "30", AllocationStatus.CONFIRMED),
        quantities("50", status=AllocationStatus.CANCELLED),
        quantities("20", status=AllocationStatus.DELIVERED),
    ]

    assert reserved_remaining(allocations) == Decimal("70")


def test_delivered_total_includes_partially_delivered_cancelled_allocations() -> None:
    allocations = [
        quantities("100", "30", AllocationStatus.CANCELLED),
        quantities("50", "50", AllocationStatus.REJECTED),
    ]

    # Rejected allocations never delivered; cancelled ones keep their history.
    assert delivered_total(allocations) == Decimal("30")


def test_remaining_need_combines_delivered_and_reserved() -> None:
    # 30 delivered of a 60 allocation: 30 delivered + 30 still reserved.
    allocations = [quantities("60", "30", AllocationStatus.CONFIRMED)]

    assert remaining_need(Decimal("100"), allocations) == Decimal("40")


@pytest.mark.parametrize(
    ("delivered", "reserved", "expected"),
    [
        ("0", "0", NeedStatus.OPEN),
        ("0", "50", NeedStatus.PARTIAL),
        ("0", "100", NeedStatus.MATCHED),
        ("30", "0", NeedStatus.PARTIAL),
        ("30", "70", NeedStatus.PARTIAL),
        ("100", "0", NeedStatus.FULFILLED),
    ],
)
def test_derive_need_status_matrix(
    delivered: str, reserved: str, expected: NeedStatus
) -> None:
    status = derive_need_status(
        quantity=Decimal("100"),
        delivered_total=Decimal(delivered),
        reserved_remaining=Decimal(reserved),
        cancelled=False,
    )

    assert status is expected


def test_cancelled_need_stays_cancelled() -> None:
    status = derive_need_status(
        quantity=Decimal("100"),
        delivered_total=Decimal("100"),
        reserved_remaining=Decimal("0"),
        cancelled=True,
    )

    assert status is NeedStatus.CANCELLED


@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (AllocationStatus.PROPOSED, AllocationStatus.CONFIRMED, True),
        (AllocationStatus.PROPOSED, AllocationStatus.CANCELLED, True),
        (AllocationStatus.PROPOSED, AllocationStatus.REJECTED, True),
        (AllocationStatus.PROPOSED, AllocationStatus.DELIVERED, False),
        (AllocationStatus.CONFIRMED, AllocationStatus.DELIVERED, True),
        (AllocationStatus.CONFIRMED, AllocationStatus.CANCELLED, True),
        (AllocationStatus.CONFIRMED, AllocationStatus.PROPOSED, False),
        (AllocationStatus.DELIVERED, AllocationStatus.CANCELLED, False),
        (AllocationStatus.CANCELLED, AllocationStatus.CONFIRMED, False),
        (AllocationStatus.REJECTED, AllocationStatus.CONFIRMED, False),
    ],
)
def test_allocation_transition_legality(
    current: AllocationStatus, target: AllocationStatus, allowed: bool
) -> None:
    assert can_transition(current, target) is allowed

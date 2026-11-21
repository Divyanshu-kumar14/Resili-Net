import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from resilinet.domain.matching import (
    NeedMatchView,
    SupplyMatchView,
    build_drafts,
    freshness_factor,
    score_candidate,
)

NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
LAT = Decimal("22.572600")
LON = Decimal("88.363900")
# About 55 km north: outside the matching radius.
FAR_LAT = Decimal("23.070000")


def need(
    suffix: int,
    *,
    remaining: str = "100",
    resource: str = "potable_water",
    unit: str = "l",
    lat: Decimal | None = LAT,
    lon: Decimal | None = LON,
) -> NeedMatchView:
    return NeedMatchView(
        id=UUID(int=suffix),
        entity_lat=lat,
        entity_lon=lon,
        resource=resource,
        unit=unit,
        remaining=Decimal(remaining),
    )


def supply(
    suffix: int,
    *,
    available: str = "100",
    resource: str = "potable_water",
    unit: str = "l",
    lat: Decimal | None = LAT,
    lon: Decimal | None = LON,
    age_hours: float = 0,
) -> SupplyMatchView:
    return SupplyMatchView(
        id=UUID(int=100 + suffix),
        entity_lat=lat,
        entity_lon=lon,
        resource=resource,
        unit=unit,
        available=Decimal(available),
        updated_at=NOW - timedelta(hours=age_hours),
    )


def test_score_at_zero_distance_with_full_stock_and_freshness_is_one() -> None:
    score = score_candidate(
        distance_km=Decimal("0"),
        available=Decimal("100"),
        remaining_need=Decimal("100"),
        freshness=Decimal("1"),
    )

    assert score == Decimal("1.000000")


def test_score_at_exactly_fifty_km_loses_only_the_distance_component() -> None:
    score = score_candidate(
        distance_km=Decimal("50"),
        available=Decimal("100"),
        remaining_need=Decimal("100"),
        freshness=Decimal("1"),
    )

    assert score == Decimal("0.400000")  # 0 + 0.25 stock + 0.15 freshness


def test_score_beyond_fifty_km_is_rejected() -> None:
    with pytest.raises(ValueError):
        score_candidate(
            distance_km=Decimal("50.001"),
            available=Decimal("100"),
            remaining_need=Decimal("100"),
            freshness=Decimal("1"),
        )


def test_freshness_is_flat_for_six_hours_then_decays_to_zero_at_24() -> None:
    assert freshness_factor(NOW - timedelta(hours=6), NOW) == Decimal("1")
    assert freshness_factor(NOW - timedelta(hours=15), NOW) == Decimal("0.5000")
    assert freshness_factor(NOW - timedelta(hours=24), NOW) == Decimal("0")
    assert freshness_factor(NOW - timedelta(hours=30), NOW) == Decimal("0")


def test_drafts_split_across_supplies_by_descending_score() -> None:
    near = supply(1, available="60")
    also_near = supply(2, available="60")

    drafts = build_drafts([need(1)], [also_near, near], NOW)

    assert sum(draft.quantity for draft in drafts) == Decimal("100.000")
    assert {draft.quantity for draft in drafts} == {Decimal("60.000"), Decimal("40.000")}


def test_drafts_are_partial_when_stock_is_short() -> None:
    drafts = build_drafts([need(1)], [supply(1, available="30")], NOW)

    assert len(drafts) == 1
    assert drafts[0].quantity == Decimal("30.000")


def test_empty_incompatible_and_unlocated_candidates_are_excluded() -> None:
    drafts = build_drafts(
        [need(1)],
        [
            supply(1, available="0"),
            supply(2, resource="food", unit="meal"),
            supply(3, unit="meal"),
            supply(4, lat=None, lon=None),
            supply(5, lat=FAR_LAT),
        ],
        NOW,
    )

    assert drafts == []


def test_need_without_coordinates_gets_no_drafts() -> None:
    assert build_drafts([need(1, lat=None, lon=None)], [supply(1)], NOW) == []


def test_earlier_ranked_need_consumes_the_shared_pool_first() -> None:
    first = need(1, remaining="80")
    second = need(2, remaining="80")

    drafts = build_drafts([first, second], [supply(1, available="100")], NOW)

    by_need = {draft.need_id: draft.quantity for draft in drafts}
    assert by_need[first.id] == Decimal("80.000")
    assert by_need[second.id] == Decimal("20.000")


def test_draft_order_is_deterministic() -> None:
    needs = [need(2), need(1)]
    supplies = [supply(2), supply(1)]

    first = build_drafts(needs, supplies, NOW)
    second = build_drafts(needs, supplies, NOW)

    assert first == second


def test_fractional_drafts_round_down_never_above_the_caps() -> None:
    drafts = build_drafts(
        [need(1, remaining="1.0015")], [supply(1, available="1.0015")], NOW
    )

    assert len(drafts) == 1
    assert drafts[0].quantity == Decimal("1.001")
    assert drafts[0].quantity <= Decimal("1.0015")


def test_drafts_never_exceed_available_stock_across_generated_cases() -> None:
    rng = random.Random(20261122)
    for _ in range(200):
        needs = [
            need(i + 1, remaining=str(rng.randint(1, 300))) for i in range(rng.randint(1, 4))
        ]
        supplies = [
            supply(i + 1, available=str(rng.randint(0, 300)))
            for i in range(rng.randint(1, 4))
        ]

        drafts = build_drafts(needs, supplies, NOW)

        for candidate in supplies:
            allocated = sum(
                draft.quantity for draft in drafts if draft.supply_id == candidate.id
            )
            assert allocated <= candidate.available
        for item in needs:
            allocated = sum(draft.quantity for draft in drafts if draft.need_id == item.id)
            assert allocated <= item.remaining

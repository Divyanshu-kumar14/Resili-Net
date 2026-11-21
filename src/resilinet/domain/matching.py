"""Deterministic candidate distance and score rules."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from uuid import UUID

EARTH_RADIUS_KM = 6371.0088
MAX_RADIUS_KM = Decimal("50")
DISTANCE_WEIGHT = Decimal("0.6")
STOCK_WEIGHT = Decimal("0.25")
FRESHNESS_WEIGHT = Decimal("0.15")
FRESH_HOURS = Decimal("6")
STALE_HOURS = Decimal("24")


def haversine_km(lat1: Decimal, lon1: Decimal, lat2: Decimal, lon2: Decimal) -> Decimal:
    """Straight-line distance in kilometres. Floats are used only for trig."""
    values = [float(lat1), float(lon1), float(lat2), float(lon2)]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("coordinates must be finite")
    phi1, phi2 = math.radians(values[0]), math.radians(values[2])
    delta_phi = math.radians(values[2] - values[0])
    delta_lambda = math.radians(values[3] - values[1])
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    distance = 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))
    if not math.isfinite(distance):
        raise ValueError("distance is not finite")
    return Decimal(str(round(distance, 3)))


def freshness_factor(updated_at: datetime, now: datetime) -> Decimal:
    """1 through six hours, linear decay to 0 at 24 hours, then 0."""
    age_hours = Decimal(str(max(0.0, (now - updated_at).total_seconds() / 3600)))
    if age_hours <= FRESH_HOURS:
        return Decimal("1")
    if age_hours >= STALE_HOURS:
        return Decimal("0")
    return ((STALE_HOURS - age_hours) / (STALE_HOURS - FRESH_HOURS)).quantize(
        Decimal("0.0001")
    )


def score_candidate(
    *,
    distance_km: Decimal,
    available: Decimal,
    remaining_need: Decimal,
    freshness: Decimal,
) -> Decimal:
    """Weighted score in [0, 1.0001]; larger is better."""
    if distance_km < 0 or available < 0 or remaining_need < 0:
        raise ValueError("score inputs cannot be negative")
    if distance_km > MAX_RADIUS_KM:
        raise ValueError("candidate is outside the maximum radius")
    stock_ratio = (
        min(Decimal("1"), available / remaining_need) if remaining_need > 0 else Decimal("0")
    )
    score = (
        DISTANCE_WEIGHT * (Decimal("1") - distance_km / MAX_RADIUS_KM)
        + STOCK_WEIGHT * stock_ratio
        + FRESHNESS_WEIGHT * freshness
    )
    return score.quantize(Decimal("0.000001"))


_QUANTUM = Decimal("0.001")


@dataclass(frozen=True, slots=True)
class NeedMatchView:
    """The match-relevant projection of one need with its entity location."""

    id: UUID
    entity_lat: Decimal | None
    entity_lon: Decimal | None
    resource: str
    unit: str
    remaining: Decimal


@dataclass(frozen=True, slots=True)
class SupplyMatchView:
    """The match-relevant projection of one supply with fresh availability."""

    id: UUID
    entity_lat: Decimal | None
    entity_lon: Decimal | None
    resource: str
    unit: str
    available: Decimal
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AllocationDraft:
    """One proposed reservation line; not yet a persisted allocation."""

    need_id: UUID
    supply_id: UUID
    quantity: Decimal
    distance_km: Decimal
    score: Decimal


def build_drafts(
    needs: list[NeedMatchView],
    supplies: list[SupplyMatchView],
    now: datetime,
) -> list[AllocationDraft]:
    """Greedily draft reservations for pre-ranked needs.

    Needs are processed in the caller's order (see
    :func:`resilinet.domain.priority.rank_needs`); each need takes from its
    best-scored candidates first. Only same-resource, same-unit,
    positive-available candidates within ``MAX_RADIUS_KM`` are scored, and a
    shared supply pool guarantees the drafts never exceed availability.
    Quantities stay ``Decimal``; floats are used only inside the haversine.
    """
    pool = {supply.id: supply.available for supply in supplies}
    drafts: list[AllocationDraft] = []
    for need in needs:
        if need.remaining <= 0 or need.entity_lat is None or need.entity_lon is None:
            continue
        remaining = need.remaining
        scored: list[tuple[Decimal, SupplyMatchView, Decimal]] = []
        for supply in supplies:
            if supply.resource != need.resource or supply.unit != need.unit:
                continue
            available = pool[supply.id]
            if available <= 0 or supply.entity_lat is None or supply.entity_lon is None:
                continue
            distance = haversine_km(
                need.entity_lat, need.entity_lon, supply.entity_lat, supply.entity_lon
            )
            if distance > MAX_RADIUS_KM:
                continue
            score = score_candidate(
                distance_km=distance,
                available=available,
                remaining_need=remaining,
                freshness=freshness_factor(supply.updated_at, now),
            )
            scored.append((score, supply, distance))
        scored.sort(key=lambda item: (-item[0], str(item[1].id)))
        for score, supply, distance in scored:
            if remaining <= 0:
                break
            quantity = min(remaining, pool[supply.id]).quantize(
                _QUANTUM, rounding=ROUND_DOWN
            )
            if quantity <= 0:
                continue
            drafts.append(
                AllocationDraft(
                    need_id=need.id,
                    supply_id=supply.id,
                    quantity=quantity,
                    distance_km=distance,
                    score=score,
                )
            )
            pool[supply.id] -= quantity
            remaining -= quantity
    return drafts

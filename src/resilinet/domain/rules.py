"""Deterministic safety rules for proposed allocation drafts.

These rules run before and after any advisory LLM critique. The critic may
approve, request one revision, or reject, but it can never change IDs,
resources, or quantities: the only revision ever applied is the
deterministic repair in :func:`revise_drafts` (drop unusable candidates,
cap quantities to the remaining need and fresh availability).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from decimal import ROUND_DOWN, Decimal
from uuid import UUID

from ..schemas import GeoConfidence, Resource
from .matching import MAX_RADIUS_KM, AllocationDraft
from .quantities import MAX_ACCEPTED_AMOUNT, MAX_ACCEPTED_LITRES

MEDICAL_RESOURCES = frozenset({Resource.MEDICINE, Resource.FIRST_AID})
#: Stems, not exact words: "injured"/"injuries"/"pregnancy" must escalate
#: just as "injury"/"pregnant" do. The plan's five example terms are the
#: core; medication/medical/asthma/fracture language is medical content too.
_MEDICAL_PATTERN = re.compile(
    r"\b(insulin|oxygen|injur\w*|pregnan\w*|dialysis|medications?|medical|"
    r"asthma|inhalers?|fractures?)\b"
    r"|\bbroken\s+(leg|arm|bone|rib)\b",
)
_QUANTUM = Decimal("0.001")


def is_medical_report(resource: Resource, text: str | None) -> bool:
    """Whether a report needs medical escalation before any stock promise."""
    if resource in MEDICAL_RESOURCES:
        return True
    if not text:
        return False
    haystack = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return _MEDICAL_PATTERN.search(haystack) is not None


@dataclass(frozen=True, slots=True)
class SupplyValidationView:
    """The rule-relevant projection of one candidate supply."""

    id: UUID
    resource: str
    unit: str
    available: Decimal
    entity_lat: Decimal | None
    entity_lon: Decimal | None


@dataclass(frozen=True, slots=True)
class DraftValidationContext:
    need_id: UUID
    resource: str
    unit: str
    remaining: Decimal
    need_lat: Decimal | None
    need_lon: Decimal | None
    need_geo_confidence: GeoConfidence
    supplies: dict[UUID, SupplyValidationView]


@dataclass(frozen=True, slots=True)
class RuleViolation:
    code: str
    detail: str
    supply_id: UUID | None = None


def validate_drafts(
    drafts: list[AllocationDraft], context: DraftValidationContext
) -> list[RuleViolation]:
    """Return every deterministic rule the drafts violate (empty = clean)."""
    violations: list[RuleViolation] = []
    seen: set[UUID] = set()
    total = Decimal("0")
    per_supply: dict[UUID, Decimal] = {}
    for draft in drafts:
        if draft.need_id != context.need_id:
            violations.append(
                RuleViolation("NEED_MISMATCH", "draft belongs to another need", draft.supply_id)
            )
            continue
        supply = context.supplies.get(draft.supply_id)
        if supply is None:
            violations.append(
                RuleViolation("UNKNOWN_SUPPLY", "candidate supply is unknown", draft.supply_id)
            )
            continue
        if draft.supply_id in seen:
            violations.append(
                RuleViolation(
                    "DUPLICATE_CANDIDATE",
                    "supply appears twice for one need",
                    draft.supply_id,
                )
            )
        seen.add(draft.supply_id)
        if supply.resource != context.resource:
            violations.append(
                RuleViolation("RESOURCE_MISMATCH", "supply resource differs", draft.supply_id)
            )
        if supply.unit != context.unit:
            violations.append(
                RuleViolation("UNIT_MISMATCH", "supply unit differs", draft.supply_id)
            )
        if draft.quantity <= 0:
            violations.append(
                RuleViolation("NON_POSITIVE_QUANTITY", "draft quantity must be positive", draft.supply_id)
            )
        if draft.distance_km < 0 or draft.distance_km > MAX_RADIUS_KM:
            violations.append(
                RuleViolation("DISTANCE_EXCEEDED", "candidate is outside 50 km", draft.supply_id)
            )
        if (
            context.need_lat is None
            or context.need_lon is None
            or context.need_geo_confidence is GeoConfidence.NONE
            or supply.entity_lat is None
            or supply.entity_lon is None
        ):
            violations.append(
                RuleViolation(
                    "AMBIGUOUS_LOCATION",
                    "need or supply location cannot support a distance match",
                    draft.supply_id,
                )
            )
        total += draft.quantity
        per_supply[draft.supply_id] = per_supply.get(draft.supply_id, Decimal("0")) + draft.quantity

    if total > context.remaining:
        violations.append(
            RuleViolation("OVER_ALLOCATION", "drafts exceed the remaining need")
        )
    for supply_id, quantity in per_supply.items():
        supply = context.supplies[supply_id]
        if quantity > supply.available:
            violations.append(
                RuleViolation(
                    "OVER_ALLOCATION", "drafts exceed supply availability", supply_id
                )
            )
    if total > MAX_ACCEPTED_AMOUNT or (
        context.resource == Resource.POTABLE_WATER.value
        and context.unit == "l"
        and total > MAX_ACCEPTED_LITRES
    ):
        violations.append(
            RuleViolation("IMPLAUSIBLE_QUANTITY", "draft total exceeds the safety cap")
        )
    return violations


def revise_drafts(
    drafts: list[AllocationDraft], context: DraftValidationContext
) -> list[AllocationDraft]:
    """Deterministically repair drafts: drop unusable lines, cap quantities.

    Supply IDs, resources, units, and distances are never altered, and no
    quantity ever grows. This is the only revision a critic verdict can
    trigger; free-text suggestions are never applied.
    """
    location_known = (
        context.need_lat is not None
        and context.need_lon is not None
        and context.need_geo_confidence is not GeoConfidence.NONE
    )
    revised: list[AllocationDraft] = []
    seen: set[UUID] = set()
    per_supply: dict[UUID, Decimal] = {}
    total = Decimal("0")
    for draft in drafts:
        supply = context.supplies.get(draft.supply_id)
        if supply is None or draft.need_id != context.need_id:
            continue
        if draft.supply_id in seen:
            continue
        if supply.resource != context.resource or supply.unit != context.unit:
            continue
        if draft.distance_km < 0 or draft.distance_km > MAX_RADIUS_KM:
            continue
        if not location_known or supply.entity_lat is None or supply.entity_lon is None:
            continue
        seen.add(draft.supply_id)
        supply_room = supply.available - per_supply.get(draft.supply_id, Decimal("0"))
        need_room = context.remaining - total
        quantity = min(draft.quantity, supply_room, need_room).quantize(
            _QUANTUM, rounding=ROUND_DOWN
        )
        if quantity <= 0:
            continue
        revised.append(replace(draft, quantity=quantity))
        per_supply[draft.supply_id] = per_supply.get(draft.supply_id, Decimal("0")) + quantity
        total += quantity
    return revised

"""Privacy-safe public projections.

Only explicitly listed fields leave the service. Sender addresses, provider
message IDs, action tokens, and private notes have no representation here.
This is the Phase 0/2 projection over ORM rows; Task 17 formalizes the
versioned HTTP API around the same field discipline.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from ..models import Allocation, Entity, Need, Supply


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _amount(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.001')):.3f}"


def _coordinate(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value.quantize(Decimal("0.000001")))


def map_snapshot(
    *,
    incident_id: UUID,
    entities: Sequence[Entity],
    needs: Sequence[Need],
    supplies: Sequence[Supply],
    allocations: Sequence[Allocation],
    available_by_supply: Mapping[UUID, Decimal],
    generated_at: datetime,
) -> dict[str, Any]:
    entity_rows = [
        {
            "id": str(entity.id),
            "name": entity.name,
            "type": entity.entity_type.value,
            "lat": _coordinate(entity.lat),
            "lon": _coordinate(entity.lon),
            "geo_confidence": entity.geo_confidence.value,
        }
        for entity in sorted(entities, key=lambda item: (item.name.casefold(), str(item.id)))
    ]
    need_rows = [
        {
            "id": str(need.id),
            "ref": need.ref,
            "entity_id": str(need.entity_id),
            "resource": need.resource.value,
            "quantity": _amount(need.quantity),
            "unit": need.unit,
            "quantity_source": need.quantity_source.value,
            "headcount": need.headcount,
            "urgency": need.urgency.value,
            "status": need.status.value,
            "verified": need.verified,
            "needs_human_review": need.needs_human_review,
            "created_at": _iso(need.created_at),
            "updated_at": _iso(need.updated_at),
        }
        for need in sorted(needs, key=lambda item: item.ref)
    ]
    supply_rows = [
        {
            "id": str(supply.id),
            "entity_id": str(supply.entity_id),
            "resource": supply.resource.value,
            "on_hand": _amount(supply.on_hand),
            "available": _amount(available_by_supply[supply.id]),
            "unit": supply.unit,
            "updated_at": _iso(supply.updated_at),
        }
        for supply in sorted(
            supplies, key=lambda item: (str(item.entity_id), item.resource.value)
        )
    ]
    need_by_id = {need.id: need for need in needs}
    allocation_rows = [
        {
            "ref": allocation.ref,
            "need_ref": need_by_id[allocation.need_id].ref,
            "need_id": str(allocation.need_id),
            "supply_id": str(allocation.supply_id),
            "resource": allocation.resource.value,
            "quantity": _amount(allocation.quantity),
            "unit": allocation.unit,
            "distance_km": _amount(allocation.distance_km),
            "status": allocation.status.value,
            "updated_at": _iso(allocation.updated_at),
        }
        for allocation in sorted(allocations, key=lambda item: item.ref)
    ]
    return {
        "incident_id": str(incident_id),
        "entities": entity_rows,
        "needs": need_rows,
        "supplies": supply_rows,
        "allocations": allocation_rows,
        "generated_at": _iso(generated_at),
    }

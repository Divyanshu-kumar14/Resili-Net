"""Privacy-safe public projections and the versioned public read service.

Only explicitly listed fields leave the service. Sender addresses, provider
message IDs, action tokens, sessions, and private notes have no representation
here. ``map_snapshot`` remains the pure projection used by the golden-path
slice; :class:`PublicService` is the Task 17 HTTP-facing reader with bounded
queries, filters, and revision-based cache validation.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, TypeVar
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db import utcnow
from ..domain.lifecycle import ACTIVE_ALLOCATION_STATUSES
from ..models import Allocation, Entity, EventLog, Incident, Need, Supply
from ..schemas import AllocationStatus, NeedStatus, Resource, Urgency

#: Public collections are bounded so an unauthenticated request cannot stream an
#: unbounded incident. The demo incident is far smaller than this.
PUBLIC_ROW_LIMIT = 500


class PublicError(Exception):
    """Base class for public-read failures that map to an HTTP response."""


class IncidentNotFound(PublicError):
    """The requested incident does not exist."""


class InvalidPublicFilter(PublicError):
    """A query filter is not one of the canonical public values."""


class PublicProjectionError(PublicError):
    """Stored data violates an invariant; the detail must not leak."""


@dataclass(frozen=True, slots=True)
class PublicFilters:
    """The resource, urgency, and need-status filters exposed publicly."""

    resource: Resource | None = None
    urgency: Urgency | None = None
    status: NeedStatus | None = None

    @classmethod
    def parse(
        cls,
        *,
        resource: str | None = None,
        urgency: str | None = None,
        status: str | None = None,
    ) -> PublicFilters:
        """Validate raw query values, rejecting unknown names with a 400."""
        try:
            return cls(
                resource=_enum_or_none(Resource, resource),
                urgency=_enum_or_none(Urgency, urgency),
                status=_enum_or_none(NeedStatus, status),
            )
        except ValueError as exc:
            raise InvalidPublicFilter("unknown public filter value") from exc

    def canonical(self) -> str:
        """A stable string for the ETag; never contains a raw request value."""
        return "|".join(
            (
                self.resource.value if self.resource else "",
                self.urgency.value if self.urgency else "",
                self.status.value if self.status else "",
            )
        )


@dataclass(frozen=True, slots=True)
class PublicSnapshot:
    """A JSON-ready public body plus its cache validator."""

    body: dict[str, Any]
    etag: str


@dataclass(frozen=True, slots=True)
class NotModified:
    """The client's validator still matches the current public revision."""

    etag: str


EnumT = TypeVar("EnumT", bound=StrEnum)


def _enum_or_none(enum_type: type[EnumT], value: str | None) -> EnumT | None:
    text = (value or "").strip()
    if not text:
        return None
    return enum_type(text)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _amount(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.001')):.3f}"


def _distance(value: Decimal) -> str:
    """Approximate distance: one decimal place, never false precision."""
    return f"{value.quantize(Decimal('0.1')):.1f}"


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
    truncated: Mapping[str, bool] | None = None,
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
    need_rows = [_need_row(need) for need in sorted(needs, key=lambda item: item.ref)]
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
        _allocation_row(allocation, need_by_id[allocation.need_id])
        for allocation in sorted(allocations, key=lambda item: item.ref)
        if allocation.need_id in need_by_id
    ]
    body: dict[str, Any] = {
        "incident_id": str(incident_id),
        "entities": entity_rows,
        "needs": need_rows,
        "supplies": supply_rows,
        "allocations": allocation_rows,
        "generated_at": _iso(generated_at),
    }
    if truncated is not None:
        body["truncated"] = {
            "entities": bool(truncated.get("entities", False)),
            "needs": bool(truncated.get("needs", False)),
            "supplies": bool(truncated.get("supplies", False)),
            "allocations": bool(truncated.get("allocations", False)),
        }
    return body


def _need_row(need: Need) -> dict[str, Any]:
    return {
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


def _allocation_row(allocation: Allocation, need: Need) -> dict[str, Any]:
    return {
        "ref": allocation.ref,
        "need_ref": need.ref,
        "need_id": str(allocation.need_id),
        "supply_id": str(allocation.supply_id),
        "resource": allocation.resource.value,
        "quantity": _amount(allocation.quantity),
        "unit": allocation.unit,
        "distance_km": _distance(allocation.distance_km),
        "status": allocation.status.value,
        "updated_at": _iso(allocation.updated_at),
    }


def etag_matches(header: str | None, etag: str) -> bool:
    """Weak comparison of an ``If-None-Match`` header against one ETag."""
    if not header:
        return False
    for candidate in header.split(","):
        value = candidate.strip()
        if value == "*":
            return True
        if value.startswith("W/"):
            value = value[2:].strip()
        if value == etag:
            return True
    return False


@dataclass(frozen=True, slots=True)
class _Loaded:
    entities: Sequence[Entity]
    needs: Sequence[Need]
    supplies: Sequence[Supply]
    allocations: Sequence[Allocation]
    available_by_supply: Mapping[UUID, Decimal]
    revision: str
    truncated: Mapping[str, bool]


class PublicService:
    """Read the public projection of one incident.

    Every collection is filtered with SQL predicates and capped at
    :data:`PUBLIC_ROW_LIMIT`; hitting the cap is reported via ``truncated``,
    never silent. Filter semantics: ``resource`` narrows needs, supplies, and
    allocations; ``urgency`` and ``status`` are need attributes, so they
    narrow needs and (through their needs) allocations only. Derived
    availability always accounts for *all* active allocations, never only
    the rows surviving a display filter. Summary counts and totals are exact
    aggregates and are not subject to the row cap.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = session_factory
        self._clock = clock or utcnow

    async def map(
        self,
        incident_id: UUID,
        filters: PublicFilters | None = None,
        if_none_match: str | None = None,
    ) -> PublicSnapshot | NotModified:
        resolved = filters or PublicFilters()
        async with self._sessions() as session:
            loaded = await self._load(session, incident_id, resolved)
        etag = self._etag("map", incident_id, resolved, loaded.revision)
        if etag_matches(if_none_match, etag):
            return NotModified(etag=etag)
        body = map_snapshot(
            incident_id=incident_id,
            entities=loaded.entities,
            needs=loaded.needs,
            supplies=loaded.supplies,
            allocations=loaded.allocations,
            available_by_supply=loaded.available_by_supply,
            generated_at=self._clock(),
            truncated=loaded.truncated,
        )
        return PublicSnapshot(body=body, etag=etag)

    async def needs(
        self,
        incident_id: UUID,
        filters: PublicFilters | None = None,
        if_none_match: str | None = None,
    ) -> PublicSnapshot | NotModified:
        resolved = filters or PublicFilters()
        async with self._sessions() as session:
            loaded = await self._load(
                session, incident_id, resolved, include_availability=False
            )
        etag = self._etag("needs", incident_id, resolved, loaded.revision)
        if etag_matches(if_none_match, etag):
            return NotModified(etag=etag)
        body = {
            "incident_id": str(incident_id),
            "needs": [_need_row(need) for need in loaded.needs],
            "truncated": bool(loaded.truncated["needs"]),
            "generated_at": _iso(self._clock()),
        }
        return PublicSnapshot(body=body, etag=etag)

    async def summary(
        self, incident_id: UUID, if_none_match: str | None = None
    ) -> PublicSnapshot | NotModified:
        filters = PublicFilters()
        async with self._sessions() as session:
            loaded = await self._load(
                session, incident_id, filters, include_availability=False
            )
            counts, totals = await self._summary_data(session, incident_id)
        etag = self._etag("summary", incident_id, filters, loaded.revision)
        if etag_matches(if_none_match, etag):
            return NotModified(etag=etag)
        body = {
            "incident_id": str(incident_id),
            "counts": counts,
            "totals": totals,
            "generated_at": _iso(self._clock()),
        }
        return PublicSnapshot(body=body, etag=etag)

    async def _load(
        self,
        session: AsyncSession,
        incident_id: UUID,
        filters: PublicFilters,
        *,
        include_availability: bool = True,
    ) -> _Loaded:
        incident = await session.get(Incident, incident_id)
        if incident is None:
            raise IncidentNotFound("incident not found")

        # Fetch one row past the cap so truncation is reported, never silent.
        probe = PUBLIC_ROW_LIMIT + 1

        entities = list(
            await session.scalars(
                sa.select(Entity)
                .where(Entity.incident_id == incident_id)
                .order_by(Entity.name, Entity.id)
                .limit(probe)
            )
        )

        need_stmt = sa.select(Need).where(Need.incident_id == incident_id)
        if filters.resource is not None:
            need_stmt = need_stmt.where(Need.resource == filters.resource)
        if filters.urgency is not None:
            need_stmt = need_stmt.where(Need.urgency == filters.urgency)
        if filters.status is not None:
            need_stmt = need_stmt.where(Need.status == filters.status)
        needs = list(
            await session.scalars(need_stmt.order_by(Need.ref).limit(probe))
        )

        supply_stmt = sa.select(Supply).where(Supply.incident_id == incident_id)
        if filters.resource is not None:
            supply_stmt = supply_stmt.where(Supply.resource == filters.resource)
        supplies = list(
            await session.scalars(
                supply_stmt.order_by(Supply.entity_id, Supply.resource).limit(probe)
            )
        )

        # Allocations are only meaningful next to their need, so they are
        # constrained to the loaded (filtered, capped) need set in every case;
        # the map can then never reference a need it does not contain.
        allocations = list(
            await session.scalars(
                sa.select(Allocation)
                .where(
                    Allocation.incident_id == incident_id,
                    Allocation.need_id.in_([need.id for need in needs]),
                )
                .order_by(Allocation.ref)
                .limit(probe)
            )
        )

        truncated = {
            "entities": len(entities) > PUBLIC_ROW_LIMIT,
            "needs": len(needs) > PUBLIC_ROW_LIMIT,
            "supplies": len(supplies) > PUBLIC_ROW_LIMIT,
            "allocations": len(allocations) > PUBLIC_ROW_LIMIT,
        }
        entities = entities[:PUBLIC_ROW_LIMIT]
        needs = needs[:PUBLIC_ROW_LIMIT]
        supplies = supplies[:PUBLIC_ROW_LIMIT]
        allocations = allocations[:PUBLIC_ROW_LIMIT]

        # Derived availability can fail closed on inconsistent stock; only the
        # map exposes it, so only the map pays for (or is poisoned by) it.
        available = (
            await self._available(session, incident_id, supplies)
            if include_availability
            else {}
        )
        revision = await self._revision(session, incident_id, loaded_rows=(
            entities,
            needs,
            supplies,
            allocations,
        ))
        return _Loaded(
            entities=entities,
            needs=needs,
            supplies=supplies,
            allocations=allocations,
            available_by_supply=available,
            revision=revision,
            truncated=truncated,
        )

    async def _summary_data(
        self, session: AsyncSession, incident_id: UUID
    ) -> tuple[dict[str, Any], dict[str, list[dict[str, str]]]]:
        """Exact incident counts and totals, independent of the row cap.

        Counts use SQL aggregates; totals sum narrow column projections in
        Python so Decimal arithmetic never passes through a float aggregate.
        """

        async def count(stmt: sa.Select[Any]) -> int:
            return int(await session.scalar(stmt) or 0)

        entities = await count(
            sa.select(sa.func.count(Entity.id)).where(
                Entity.incident_id == incident_id
            )
        )
        supplies = await count(
            sa.select(sa.func.count(Supply.id)).where(
                Supply.incident_id == incident_id
            )
        )
        allocations = await count(
            sa.select(sa.func.count(Allocation.id)).where(
                Allocation.incident_id == incident_id
            )
        )
        by_status: dict[str, int] = {}
        status_rows = await session.execute(
            sa.select(Need.status, sa.func.count(Need.id))
            .where(Need.incident_id == incident_id)
            .group_by(Need.status)
        )
        for status, amount in status_rows:
            by_status[status.value] = int(amount)
        by_resource: dict[str, int] = {}
        resource_rows = await session.execute(
            sa.select(Need.resource, sa.func.count(Need.id))
            .where(Need.incident_id == incident_id)
            .group_by(Need.resource)
        )
        for resource, amount in resource_rows:
            by_resource[resource.value] = int(amount)
        counts = {
            "entities": entities,
            "needs": sum(by_status.values()),
            "supplies": supplies,
            "allocations": allocations,
            "needs_by_status": dict(sorted(by_status.items())),
            "needs_by_resource": dict(sorted(by_resource.items())),
        }

        need_rows = list(
            await session.execute(
                sa.select(Need.resource, Need.unit, Need.quantity, Need.status).where(
                    Need.incident_id == incident_id
                )
            )
        )
        allocation_rows = list(
            await session.execute(
                sa.select(
                    Need.resource,
                    Need.unit,
                    Need.status,
                    Allocation.status,
                    Allocation.quantity,
                    Allocation.delivered_quantity,
                )
                .join(Need, Allocation.need_id == Need.id)
                .where(Allocation.incident_id == incident_id)
            )
        )
        return counts, _totals(need_rows, allocation_rows)

    async def _available(
        self,
        session: AsyncSession,
        incident_id: UUID,
        supplies: Sequence[Supply],
    ) -> dict[UUID, Decimal]:
        """Derived availability across every active allocation, filter-independent."""
        supply_ids = [supply.id for supply in supplies]
        reserved: dict[UUID, Decimal] = defaultdict(lambda: Decimal("0"))
        if supply_ids:
            rows = await session.execute(
                sa.select(
                    Allocation.supply_id,
                    Allocation.quantity,
                    Allocation.delivered_quantity,
                ).where(
                    Allocation.incident_id == incident_id,
                    Allocation.supply_id.in_(supply_ids),
                    Allocation.status.in_(sorted(ACTIVE_ALLOCATION_STATUSES)),
                )
            )
            for supply_id, quantity, delivered in rows:
                reserved[supply_id] += quantity - delivered
        available: dict[UUID, Decimal] = {}
        for supply in supplies:
            value = supply.on_hand - reserved[supply.id]
            if value < 0:
                raise PublicProjectionError("negative available stock")
            available[supply.id] = value
        return available

    async def _revision(
        self,
        session: AsyncSession,
        incident_id: UUID,
        *,
        loaded_rows: tuple[Sequence[Any], ...],
    ) -> str:
        """Combine row revisions and the audit-event revision.

        The validator assumes mutations go through the service layer, which
        bumps ``updated_at`` and appends an audit event for every committed
        change. Direct database repair that rewrites a public field without
        touching either is outside that invariant and may not invalidate
        cached responses.
        """
        parts: list[str] = []
        for rows in loaded_rows:
            parts.append(_row_revision(rows))
        event_count = await session.scalar(
            sa.select(sa.func.count(EventLog.id)).where(
                EventLog.incident_id == incident_id
            )
        )
        event_latest = await session.scalar(
            sa.select(EventLog.created_at)
            .where(EventLog.incident_id == incident_id)
            .order_by(EventLog.created_at.desc())
            .limit(1)
        )
        parts.append(
            f"{int(event_count or 0)}:{event_latest.isoformat() if event_latest else '-'}"
        )
        return "|".join(parts)

    @staticmethod
    def _etag(
        kind: str, incident_id: UUID, filters: PublicFilters, revision: str
    ) -> str:
        material = f"{kind}|{incident_id}|{filters.canonical()}|{revision}"
        digest = hashlib.sha256(material.encode()).hexdigest()
        return f'"{digest}"'


def _row_revision(rows: Sequence[Any]) -> str:
    """Count plus the newest ``updated_at`` for one loaded collection."""
    latest: datetime | None = None
    for row in rows:
        value = getattr(row, "updated_at", None)
        if isinstance(value, datetime) and (latest is None or value > latest):
            latest = value
    return f"{len(rows)}:{latest.isoformat() if latest else '-'}"


def _totals(
    need_rows: Sequence[Any], allocation_rows: Sequence[Any]
) -> dict[str, list[dict[str, str]]]:
    """Requested, delivered, reserved, and unfulfilled totals by resource/unit.

    Cancelled needs are no longer a request, so they contribute nothing to
    ``requested``/``unfulfilled``; quantities already delivered against them
    still count in ``delivered``, because that history is never rewritten.
    Totals are Decimal strings with three places so a client never receives
    binary-float arithmetic.
    """
    buckets: dict[tuple[Resource, str], dict[str, Decimal]] = defaultdict(
        lambda: {
            "requested": Decimal("0"),
            "delivered": Decimal("0"),
            "delivered_open": Decimal("0"),
            "reserved": Decimal("0"),
        }
    )
    for resource, unit, quantity, status in need_rows:
        if status is NeedStatus.CANCELLED:
            continue
        buckets[(resource, unit)]["requested"] += quantity
    for resource, unit, need_status, status, quantity, delivered in allocation_rows:
        if status is AllocationStatus.REJECTED:
            continue
        bucket = buckets[(resource, unit)]
        bucket["delivered"] += delivered
        if need_status is not NeedStatus.CANCELLED:
            bucket["delivered_open"] += delivered
            if status in ACTIVE_ALLOCATION_STATUSES:
                bucket["reserved"] += quantity - delivered

    def rows(kind: str) -> list[dict[str, str]]:
        return [
            {
                "resource": resource.value,
                "unit": unit,
                "quantity": _amount(values[kind]),
            }
            for (resource, unit), values in sorted(
                buckets.items(), key=lambda item: (item[0][0].value, item[0][1])
            )
        ]

    unfulfilled = [
        {
            "resource": resource.value,
            "unit": unit,
            "quantity": _amount(
                max(Decimal("0"), values["requested"] - values["delivered_open"])
            ),
        }
        for (resource, unit), values in sorted(
            buckets.items(), key=lambda item: (item[0][0].value, item[0][1])
        )
    ]
    return {
        "requested": rows("requested"),
        "delivered": rows("delivered"),
        "reserved": rows("reserved"),
        "unfulfilled": unfulfilled,
    }


__all__ = [
    "PUBLIC_ROW_LIMIT",
    "IncidentNotFound",
    "InvalidPublicFilter",
    "NotModified",
    "PublicError",
    "PublicFilters",
    "PublicProjectionError",
    "PublicService",
    "PublicSnapshot",
    "etag_matches",
    "map_snapshot",
]

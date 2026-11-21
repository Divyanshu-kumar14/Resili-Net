"""Allocation ledgers: the Phase 0 in-memory slice and its durable replacement.

The durable :class:`AllocationService` (Task 10) persists locked database
rows and preserves the invariants the slice encoded: available stock is
never negative, reservations are active while proposed or confirmed, and a
need is never persisted below its delivered total. Every stock mutation
locks its aggregate rows in a deterministic order, recomputes availability
from committed rows, writes audit events, and never trusts stale scores.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.duplicates import (
    DuplicateDecision,
    DuplicateKind,
    DuplicateNeedView,
    find_duplicate,
)
from ..domain.lifecycle import (
    AllocationQuantities,
    can_transition,
    delivered_total,
    derive_need_status,
    reserved_remaining,
)
from ..domain.matching import (
    AllocationDraft,
    NeedMatchView,
    SupplyMatchView,
    build_drafts,
)
from ..domain.priority import NeedPriorityView, rank_needs
from ..domain.quantities import (
    QuantityError,
    QuantitySource,
    canonical_unit,
    estimate_need,
    to_canonical,
    to_stated,
)
from ..domain.references import new_reference, parse_reference
from ..models import Allocation, Entity, Message, Need, Sender, Supply
from ..schemas import (
    AllocationStatus,
    NeedStatus,
    ParsedEmail,
    ParsedReport,
    ReportKind,
    Resource,
    Urgency,
)
from .events import record_event

ACTIVE_ALLOCATION_STATUSES = frozenset(
    {AllocationStatus.PROPOSED, AllocationStatus.CONFIRMED}
)


class AvailabilityError(ValueError):
    """An operation would make a stock invariant negative."""


# -- durable transactional engine (Task 10) -------------------------------------

_QUANTUM = Decimal("0.001")


class AllocationStateError(ValueError):
    """A lifecycle transition or delivery quantity is not legal."""


@dataclass(frozen=True, slots=True)
class CommitResult:
    allocations: tuple[Allocation, ...]
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    supply: Supply
    cancelled: tuple[Allocation, ...]


@dataclass(frozen=True, slots=True)
class RegistrationFailure:
    report_id: UUID
    resource: Resource
    reason: str


@dataclass(frozen=True, slots=True)
class DuplicateCandidateHit:
    need: Need
    existing_id: UUID


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    created_needs: tuple[Need, ...]
    corroborated: tuple[Need, ...]
    duplicate_candidates: tuple[DuplicateCandidateHit, ...]
    snapshots: tuple[SnapshotResult, ...]
    failures: tuple[RegistrationFailure, ...]
    #: Corroborating reports that were themselves medical: the existing need
    #: still needs a medical review case even though no new need was created.
    medical_corroborated: tuple[Need, ...] = ()


def _quantities(rows: Sequence[Allocation]) -> list[AllocationQuantities]:
    return [
        AllocationQuantities(
            quantity=row.quantity,
            delivered_quantity=row.delivered_quantity,
            status=row.status,
        )
        for row in rows
    ]


def _ref_number(ref: str) -> int:
    try:
        return parse_reference(ref)[1]
    except Exception:
        return 0


def _newest_first(rows: Sequence[Allocation]) -> list[Allocation]:
    return sorted(rows, key=lambda row: (row.created_at, _ref_number(row.ref)), reverse=True)


def _cancellation_order(rows: Sequence[Allocation]) -> list[Allocation]:
    """Newest proposals first, then newest confirmed allocations."""
    active = [row for row in rows if row.status in (AllocationStatus.PROPOSED, AllocationStatus.CONFIRMED)]
    proposals = _newest_first(
        [row for row in active if row.status is AllocationStatus.PROPOSED]
    )
    confirmed = _newest_first(
        [row for row in active if row.status is AllocationStatus.CONFIRMED]
    )
    return proposals + confirmed


class AllocationService:
    """Durable, transactional reservation engine.

    All methods run inside the caller's transaction. Reservation commits lock
    the need and candidate supply rows in sorted ID order, recompute
    availability from committed rows, and write allocations, events, and
    status changes atomically — never reserving from stale scores. Role and
    token authorization are enforced by the action layer (Phase 3) inside
    these same transactions; this service enforces state, quantity, and
    stock invariants.
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- reads --------------------------------------------------------------

    async def allocations_for_need(
        self, session: AsyncSession, need_id: UUID
    ) -> list[Allocation]:
        rows = await session.scalars(
            sa.select(Allocation)
            .where(Allocation.need_id == need_id)
            .order_by(Allocation.created_at, Allocation.ref)
        )
        return list(rows)

    async def allocations_for_supply(
        self, session: AsyncSession, supply_id: UUID
    ) -> list[Allocation]:
        rows = await session.scalars(
            sa.select(Allocation)
            .where(Allocation.supply_id == supply_id)
            .order_by(Allocation.created_at, Allocation.ref)
        )
        return list(rows)

    async def available(self, session: AsyncSession, supply: Supply) -> Decimal:
        """On-hand minus actively reserved; a negative result is an error."""
        reserved = reserved_remaining(
            _quantities(await self.allocations_for_supply(session, supply.id))
        )
        available = supply.on_hand - reserved
        if available < 0:
            raise AvailabilityError(
                f"supply {supply.id} would have negative available stock"
            )
        return available

    async def remaining_for_need(self, session: AsyncSession, need: Need) -> Decimal:
        rows = _quantities(await self.allocations_for_need(session, need.id))
        delivered = delivered_total(rows)
        reserved = reserved_remaining(rows)
        remaining = need.quantity - delivered - reserved
        if remaining < 0:
            raise AvailabilityError(f"need {need.ref} is over-reserved")
        return remaining

    # -- need creation --------------------------------------------------------

    async def create_need(
        self,
        session: AsyncSession,
        *,
        incident_id: UUID,
        entity_id: UUID,
        resource: Resource,
        quantity: Decimal,
        unit: str,
        quantity_source: QuantitySource,
        headcount: int | None,
        urgency: Urgency,
        verified: bool,
        needs_human_review: bool = False,
        source_report_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> Need:
        quantity = quantity.quantize(_QUANTUM, rounding=ROUND_DOWN)
        if quantity <= 0:
            raise AllocationStateError(
                "need quantity must be at least 0.001 in canonical resolution"
            )
        now = self._clock()
        need = Need(
            id=uuid4(),
            incident_id=incident_id,
            entity_id=entity_id,
            source_report_id=source_report_id,
            ref=await new_reference(session, "RN"),
            resource=resource,
            quantity=quantity.quantize(_QUANTUM, rounding=ROUND_DOWN),
            unit=unit,
            quantity_source=quantity_source,
            headcount=headcount,
            urgency=urgency,
            status=NeedStatus.OPEN,
            verified=verified,
            needs_human_review=needs_human_review,
            created_at=now,
            updated_at=now,
        )
        session.add(need)
        await session.flush()
        await record_event(
            session,
            incident_id=incident_id,
            event_type="need.created",
            actor_id=actor_id,
            aggregate_type="need",
            aggregate_id=need.id,
            payload={
                "need_ref": need.ref,
                "resource": resource.value,
                "quantity": str(need.quantity),
                "unit": unit,
                "quantity_source": quantity_source.value,
                "report_id": str(source_report_id) if source_report_id else None,
            },
        )
        return need

    # -- report registration ----------------------------------------------------

    async def register_reports(
        self,
        session: AsyncSession,
        *,
        message: Message,
        sender: Sender,
        parsed: ParsedEmail,
        entity_id: UUID | None,
        report_ids: Sequence[UUID],
        medical_flags: Sequence[bool] = (),
    ) -> RegistrationResult:
        """Turn persisted reports into needs and supply snapshots.

        Quantities are canonicalized here (or kept as stated units for
        resources without a canonical unit); a line that cannot be accepted
        becomes a registration failure for a review case rather than a
        clamped value. A strong duplicate corroborates the existing need
        without overwriting its quantity; a proximity-only candidate still
        creates its need and is reported for operator review.
        """
        created: list[Need] = []
        corroborated: list[Need] = []
        medical_corroborated: list[Need] = []
        candidates: list[DuplicateCandidateHit] = []
        snapshots: list[SnapshotResult] = []
        failures: list[RegistrationFailure] = []
        seen_supply_keys: set[tuple[UUID, Resource]] = set()
        if entity_id is None:
            return RegistrationResult((), (), (), (), ())
        entity = await session.get(Entity, entity_id)
        if entity is None:  # pragma: no cover - persistence guarantees the row
            return RegistrationResult((), (), (), (), ())

        for index, report in enumerate(parsed.reports):
            report_id = report_ids[index] if index < len(report_ids) else None
            medical = medical_flags[index] if index < len(medical_flags) else False
            if parsed.context.kind is ReportKind.SUPPLY:
                try:
                    amount, unit = self._supply_quantity(report)
                except QuantityError as exc:
                    if report_id is not None:
                        failures.append(
                            RegistrationFailure(report_id, report.resource, str(exc))
                        )
                    continue
                supply_key = (entity.id, report.resource)
                if supply_key in seen_supply_keys:
                    # One absolute snapshot per (entity, resource) per
                    # message; a second line cannot silently overwrite it.
                    if report_id is not None:
                        failures.append(
                            RegistrationFailure(
                                report_id,
                                report.resource,
                                "duplicate supply line for one entity in a "
                                "single message",
                            )
                        )
                    continue
                seen_supply_keys.add(supply_key)
                try:
                    snapshots.append(
                        await self.apply_supply_snapshot(
                            session,
                            incident_id=message.incident_id,
                            entity_id=entity.id,
                            resource=report.resource,
                            quantity=amount,
                            unit=unit,
                            source_report_id=report_id,
                            actor_id=sender.id,
                        )
                    )
                except AvailabilityError as exc:
                    if report_id is not None:
                        failures.append(
                            RegistrationFailure(report_id, report.resource, str(exc))
                        )
                continue

            if report_id is not None:
                # A retried job re-registers nothing: the need this report
                # already produced is returned for (idempotent) matching.
                existing_need = await session.scalar(
                    sa.select(Need).where(Need.source_report_id == report_id)
                )
                if existing_need is not None:
                    created.append(existing_need)
                    continue
            try:
                amount, unit, source = self._need_quantity(
                    report, parsed.context.headcount
                )
            except QuantityError as exc:
                if report_id is not None:
                    failures.append(
                        RegistrationFailure(report_id, report.resource, str(exc))
                    )
                continue
            decision = await self._find_duplicate(
                session, message.incident_id, entity, report, unit
            )
            if decision.kind is DuplicateKind.DUPLICATE and decision.existing_id is not None:
                existing = await session.get(Need, decision.existing_id)
                if existing is not None:
                    corroborated.append(existing)
                    if medical:
                        medical_corroborated.append(existing)
                    await record_event(
                        session,
                        incident_id=message.incident_id,
                        event_type="need.corroborated",
                        actor_id=sender.id,
                        aggregate_type="need",
                        aggregate_id=existing.id,
                        payload={
                            "need_ref": existing.ref,
                            "report_id": str(report_id) if report_id else None,
                        },
                    )
                    continue
            need = await self.create_need(
                session,
                incident_id=message.incident_id,
                entity_id=entity.id,
                resource=report.resource,
                quantity=amount,
                unit=unit,
                quantity_source=source,
                headcount=parsed.context.headcount,
                urgency=parsed.context.urgency,
                verified=sender.verified,
                needs_human_review=medical,
                source_report_id=report_id,
                actor_id=sender.id,
            )
            created.append(need)
            if decision.kind is DuplicateKind.REVIEW_CANDIDATE and decision.existing_id is not None:
                candidates.append(
                    DuplicateCandidateHit(need=need, existing_id=decision.existing_id)
                )
        return RegistrationResult(
            created_needs=tuple(created),
            corroborated=tuple(corroborated),
            duplicate_candidates=tuple(candidates),
            snapshots=tuple(snapshots),
            failures=tuple(failures),
            medical_corroborated=tuple(medical_corroborated),
        )

    def _need_quantity(
        self, report: ParsedReport, headcount: int | None
    ) -> tuple[Decimal, str, QuantitySource]:
        """Resolve a NEED line: stated canonical, stated raw, or an estimate."""
        if canonical_unit(report.resource) is not None:
            if report.quantity is None or report.quantity == 0:
                estimated = estimate_need(report.resource, headcount)
                if estimated is None:
                    raise QuantityError(
                        "quantity is missing and cannot be estimated"
                    )
                return estimated.amount, estimated.unit.value, QuantitySource.ESTIMATED
            converted = to_canonical(report.resource, report.quantity, report.unit)
            return converted.amount, converted.unit.value, QuantitySource.STATED
        if report.quantity is None or report.quantity == 0:
            raise QuantityError("quantity is missing")
        stated = to_stated(report.resource, report.quantity, report.unit)
        return stated.amount, stated.unit, QuantitySource.STATED

    def _supply_quantity(self, report: ParsedReport) -> tuple[Decimal, str]:
        """Resolve a SUPPLY line; zero is a valid empty snapshot."""
        if report.quantity is None:
            raise QuantityError("quantity is missing")
        if canonical_unit(report.resource) is not None:
            converted = to_canonical(report.resource, report.quantity, report.unit)
            return converted.amount, converted.unit.value
        stated = to_stated(report.resource, report.quantity, report.unit)
        return stated.amount, stated.unit

    async def _find_duplicate(
        self,
        session: AsyncSession,
        incident_id: UUID,
        entity: Entity,
        report: ParsedReport,
        unit: str,
    ) -> DuplicateDecision:
        rows = await session.execute(
            sa.select(Need, Entity)
            .join(Entity, Entity.id == Need.entity_id)
            .where(Need.incident_id == incident_id)
        )
        views = [
            DuplicateNeedView(
                need_id=need.id,
                entity_id=need.entity_id,
                entity_name=owner.name,
                resource=need.resource.value,
                unit=need.unit,
                lat=owner.lat,
                lon=owner.lon,
                status=need.status,
                created_at=need.created_at,
            )
            for need, owner in rows
        ]
        incoming = DuplicateNeedView(
            need_id=uuid4(),
            entity_id=entity.id,
            entity_name=entity.name,
            resource=report.resource.value,
            unit=unit,
            lat=entity.lat,
            lon=entity.lon,
            status=NeedStatus.OPEN,
            created_at=self._clock(),
        )
        return find_duplicate(incoming, views, self._clock())

    # -- matching snapshots -----------------------------------------------------

    async def drafts_for_need(
        self, session: AsyncSession, need_id: UUID, now: datetime
    ) -> list[AllocationDraft]:
        need = await session.get(Need, need_id)
        if need is None:
            raise AllocationStateError(f"need {need_id} does not exist")
        entity = await session.get(Entity, need.entity_id)
        if entity is None:  # pragma: no cover - FK guarantees the row
            return []
        remaining = await self.remaining_for_need(session, need)
        need_view = NeedMatchView(
            id=need.id,
            entity_lat=entity.lat,
            entity_lon=entity.lon,
            resource=need.resource.value,
            unit=need.unit,
            remaining=remaining,
        )
        return build_drafts([need_view], await self.supply_views(session, need), now)

    async def supply_views(
        self, session: AsyncSession, need: Need
    ) -> list[SupplyMatchView]:
        rows = await session.execute(
            sa.select(Supply, Entity)
            .join(Entity, Entity.id == Supply.entity_id)
            .where(
                Supply.incident_id == need.incident_id,
                Supply.resource == need.resource,
            )
        )
        views: list[SupplyMatchView] = []
        for supply, owner in rows:
            views.append(
                SupplyMatchView(
                    id=supply.id,
                    entity_lat=owner.lat,
                    entity_lon=owner.lon,
                    resource=supply.resource.value,
                    unit=supply.unit,
                    available=await self.available(session, supply),
                    updated_at=supply.updated_at,
                )
            )
        return views

    async def ranked_open_needs(
        self, session: AsyncSession, incident_id: UUID, resource: Resource, now: datetime
    ) -> list[Need]:
        """Open, matchable needs for one resource in deterministic priority order."""
        rows = list(
            await session.scalars(
                sa.select(Need).where(
                    Need.incident_id == incident_id,
                    Need.resource == resource,
                    Need.status.in_((NeedStatus.OPEN, NeedStatus.PARTIAL)),
                    Need.needs_human_review.is_(False),
                )
            )
        )
        matchable: list[Need] = []
        for need in rows:
            if await self.remaining_for_need(session, need) > 0:
                matchable.append(need)
        ranked = rank_needs(
            [
                NeedPriorityView(
                    id=need.id,
                    urgency=need.urgency,
                    headcount=need.headcount,
                    created_at=need.created_at,
                    verified=need.verified,
                    is_medical=need.needs_human_review,
                )
                for need in matchable
            ],
            now,
        )
        by_id = {need.id: need for need in matchable}
        return [by_id[view.id] for view in ranked]

    # -- reservation commit -------------------------------------------------------

    async def commit(
        self,
        session: AsyncSession,
        *,
        need_id: UUID,
        drafts: Sequence[AllocationDraft],
        critic_verdict: str,
        actor_id: UUID | None = None,
    ) -> CommitResult:
        """Commit approved drafts under row locks with fresh availability.

        Drafts are advisory: locked rows are re-read, a draft whose supply no
        longer matches the need's resource/unit is rejected, and quantities
        are capped by fresh remaining need and fresh availability so stock
        can never be over-reserved.
        """
        now = self._clock()
        need = await self._locked_need(session, need_id)
        if need.status is NeedStatus.CANCELLED:
            raise AllocationStateError(
                f"need {need.ref} is cancelled and cannot reserve stock"
            )
        if need.needs_human_review:
            # Medical/escalated needs never receive automatic commitment.
            return CommitResult(allocations=(), quantity=Decimal("0"))
        own_drafts = [draft for draft in drafts if draft.need_id == need_id]
        supply_ids = sorted({draft.supply_id for draft in own_drafts}, key=str)
        supplies: dict[UUID, Supply] = {}
        if supply_ids:
            locked = await session.scalars(
                sa.select(Supply)
                .where(Supply.id.in_(supply_ids))
                .order_by(Supply.id)
                .with_for_update()
            )
            supplies = {supply.id: supply for supply in locked}

        quantities_by_supply: dict[UUID, list[AllocationQuantities]] = {}
        for supply_id in supply_ids:
            quantities_by_supply[supply_id] = _quantities(
                await self.allocations_for_supply(session, supply_id)
            )
        fresh_remaining = await self.remaining_for_need(session, need)

        created: list[Allocation] = []
        for draft in own_drafts:
            if fresh_remaining <= 0:
                await self._record_adjusted(
                    session, need, draft,
                    committed=Decimal("0"), reason="need_already_filled",
                    actor_id=actor_id,
                )
                continue
            supply = supplies.get(draft.supply_id)
            if supply is None:
                await self._record_adjusted(
                    session, need, draft,
                    committed=Decimal("0"), reason="supply_missing",
                    actor_id=actor_id,
                )
                continue
            if supply.resource is not need.resource or supply.unit != need.unit:
                # Stale candidate: no longer a match for this need.
                await self._record_adjusted(
                    session, need, draft,
                    committed=Decimal("0"), reason="stale_candidate",
                    actor_id=actor_id,
                )
                continue
            supply_quantities = quantities_by_supply[draft.supply_id]
            fresh_available = supply.on_hand - reserved_remaining(supply_quantities)
            quantity = min(draft.quantity, fresh_available, fresh_remaining)
            if quantity <= 0:
                await self._record_adjusted(
                    session, need, draft,
                    committed=Decimal("0"), reason="no_fresh_availability",
                    actor_id=actor_id,
                )
                continue
            quantity = quantity.quantize(_QUANTUM, rounding=ROUND_DOWN)
            allocation = Allocation(
                id=uuid4(),
                incident_id=need.incident_id,
                need_id=need.id,
                supply_id=supply.id,
                ref=await new_reference(session, "AL"),
                resource=need.resource,
                quantity=quantity,
                unit=need.unit,
                delivered_quantity=Decimal("0"),
                distance_km=draft.distance_km,
                status=AllocationStatus.PROPOSED,
                critic_verdict=critic_verdict,
                created_at=now,
                updated_at=now,
            )
            session.add(allocation)
            await session.flush()
            created.append(allocation)
            supply_quantities.append(
                AllocationQuantities(
                    quantity=quantity,
                    delivered_quantity=Decimal("0"),
                    status=AllocationStatus.PROPOSED,
                )
            )
            fresh_remaining -= quantity
            await record_event(
                session,
                incident_id=need.incident_id,
                event_type="allocation.proposed",
                actor_id=actor_id,
                aggregate_type="allocation",
                aggregate_id=allocation.id,
                payload={
                    "allocation_ref": allocation.ref,
                    "need_ref": need.ref,
                    "supply_id": str(supply.id),
                    "quantity": str(quantity),
                    "unit": need.unit,
                    "distance_km": str(draft.distance_km),
                },
            )
            if quantity < draft.quantity:
                await self._record_adjusted(
                    session, need, draft,
                    committed=quantity, reason="fresh_availability",
                    actor_id=actor_id,
                )
        await self.refresh_need_status(session, need)
        return CommitResult(
            allocations=tuple(created),
            quantity=sum((row.quantity for row in created), Decimal("0")),
        )

    # -- supply snapshots and reconciliation ---------------------------------------

    async def apply_supply_snapshot(
        self,
        session: AsyncSession,
        *,
        incident_id: UUID,
        entity_id: UUID,
        resource: Resource,
        quantity: Decimal,
        unit: str,
        source_report_id: UUID | None,
        actor_id: UUID | None = None,
    ) -> SnapshotResult:
        """Store an absolute on-hand snapshot and reconcile reservations.

        If the new figure cannot support active reservations, only the
        excess newest allocations are cancelled (proposals first); delivered
        history and unaffected allocations are never altered.
        """
        now = self._clock()
        supply = await session.scalar(
            sa.select(Supply)
            .where(
                Supply.incident_id == incident_id,
                Supply.entity_id == entity_id,
                Supply.resource == resource,
            )
            .with_for_update()
        )
        if supply is None:
            supply = Supply(
                id=uuid4(),
                incident_id=incident_id,
                entity_id=entity_id,
                source_report_id=source_report_id,
                resource=resource,
                on_hand=quantity.quantize(_QUANTUM, rounding=ROUND_DOWN),
                unit=unit,
                created_at=now,
                updated_at=now,
            )
            session.add(supply)
        else:
            if supply.unit != unit:
                reserved = reserved_remaining(
                    _quantities(await self.allocations_for_supply(session, supply.id))
                )
                if reserved > 0:
                    raise AvailabilityError(
                        f"snapshot unit {unit!r} cannot replace {supply.unit!r} "
                        f"while {reserved} is reserved in the old unit"
                    )
            supply.on_hand = quantity.quantize(_QUANTUM, rounding=ROUND_DOWN)
            supply.unit = unit
            supply.source_report_id = source_report_id or supply.source_report_id
            supply.updated_at = now
        await session.flush()
        await record_event(
            session,
            incident_id=incident_id,
            event_type="supply.snapshot",
            actor_id=actor_id,
            aggregate_type="supply",
            aggregate_id=supply.id,
            payload={
                "entity_id": str(entity_id),
                "resource": resource.value,
                "on_hand": str(supply.on_hand),
                "unit": unit,
                "report_id": str(source_report_id) if source_report_id else None,
            },
        )

        rows = await self.allocations_for_supply(session, supply.id)
        reserved = reserved_remaining(_quantities(rows))
        cancelled: list[Allocation] = []
        if supply.on_hand < reserved:
            excess = reserved - supply.on_hand
            affected_needs: list[UUID] = []
            for allocation in _cancellation_order(rows):
                if excess <= 0:
                    break
                remaining = allocation.quantity - allocation.delivered_quantity
                await self._cancel_row(
                    session, allocation, reason="supply_snapshot", actor_id=actor_id
                )
                cancelled.append(allocation)
                excess -= remaining
                if allocation.need_id not in affected_needs:
                    affected_needs.append(allocation.need_id)
            for affected_id in affected_needs:
                affected = await session.get(Need, affected_id)
                if affected is not None:
                    await self.refresh_need_status(session, affected)
        return SnapshotResult(supply=supply, cancelled=tuple(cancelled))

    # -- need/allocation lifecycle ---------------------------------------------------

    async def update_need(
        self,
        session: AsyncSession,
        *,
        need_id: UUID,
        new_quantity: Decimal,
        actor_id: UUID | None = None,
    ) -> Need:
        """Revise a need quantity, cancelling only the newest excess.

        A revision below the delivered total is always rejected — including
        for operators — and is never clamped.
        """
        need = await self._locked_need(session, need_id)
        rows = await self.allocations_for_need(session, need.id)
        quantities = _quantities(rows)
        delivered = delivered_total(quantities)
        if new_quantity < delivered:
            raise AvailabilityError(
                f"need quantity {new_quantity} is below delivered total {delivered}"
            )
        old_quantity = need.quantity
        need.quantity = new_quantity.quantize(_QUANTUM, rounding=ROUND_DOWN)
        need.updated_at = self._clock()
        await session.flush()

        reserved = reserved_remaining(quantities)
        allowed_reserved = need.quantity - delivered
        if reserved > allowed_reserved:
            excess = reserved - allowed_reserved
            for allocation in _cancellation_order(rows):
                if excess <= 0:
                    break
                remaining = allocation.quantity - allocation.delivered_quantity
                await self._cancel_row(
                    session, allocation, reason="need_reduced", actor_id=actor_id
                )
                excess -= remaining
        await record_event(
            session,
            incident_id=need.incident_id,
            event_type="need.updated",
            actor_id=actor_id,
            aggregate_type="need",
            aggregate_id=need.id,
            payload={
                "need_ref": need.ref,
                "old_quantity": str(old_quantity),
                "new_quantity": str(need.quantity),
            },
        )
        await self.refresh_need_status(session, need)
        return need

    async def confirm_allocation(
        self,
        session: AsyncSession,
        *,
        allocation_id: UUID,
        actor_id: UUID | None = None,
    ) -> Allocation:
        need, _, allocation = await self._locked_trio(session, allocation_id)
        if not can_transition(allocation.status, AllocationStatus.CONFIRMED):
            raise AllocationStateError(
                f"allocation {allocation.ref} cannot be confirmed from "
                f"{allocation.status.value}"
            )
        allocation.status = AllocationStatus.CONFIRMED
        allocation.updated_at = self._clock()
        await session.flush()
        await record_event(
            session,
            incident_id=need.incident_id,
            event_type="allocation.confirmed",
            actor_id=actor_id,
            aggregate_type="allocation",
            aggregate_id=allocation.id,
            payload={"allocation_ref": allocation.ref, "need_ref": need.ref},
        )
        return allocation

    async def cancel_allocation(
        self,
        session: AsyncSession,
        *,
        allocation_id: UUID,
        actor_id: UUID | None = None,
        reason: str = "cancelled",
    ) -> Allocation:
        need, _, allocation = await self._locked_trio(session, allocation_id)
        if not can_transition(allocation.status, AllocationStatus.CANCELLED):
            raise AllocationStateError(
                f"allocation {allocation.ref} cannot be cancelled from "
                f"{allocation.status.value}"
            )
        await self._cancel_row(session, allocation, reason=reason, actor_id=actor_id)
        await self.refresh_need_status(session, need)
        return allocation

    async def cancel_need(
        self,
        session: AsyncSession,
        *,
        need_id: UUID,
        actor_id: UUID | None = None,
    ) -> Need:
        need = await self._locked_need(session, need_id)
        if need.status is NeedStatus.CANCELLED:
            return need
        for allocation in _cancellation_order(
            await self.allocations_for_need(session, need.id)
        ):
            await self._cancel_row(
                session, allocation, reason="need_cancelled", actor_id=actor_id
            )
        need.status = NeedStatus.CANCELLED
        need.updated_at = self._clock()
        await session.flush()
        await record_event(
            session,
            incident_id=need.incident_id,
            event_type="need.cancelled",
            actor_id=actor_id,
            aggregate_type="need",
            aggregate_id=need.id,
            payload={"need_ref": need.ref},
        )
        return need

    async def record_delivery(
        self,
        session: AsyncSession,
        *,
        allocation_id: UUID,
        quantity: Decimal,
        actor_id: UUID | None = None,
    ) -> Allocation:
        """Consume on-hand stock for an explicitly reported delivery.

        Only a confirmed allocation can receive deliveries. A partial
        delivery leaves the allocation confirmed; a full delivery marks it
        delivered. On-hand stock is decremented exactly once per report.
        """
        need, supply, allocation = await self._locked_trio(session, allocation_id)
        if allocation.unit != supply.unit:
            raise AllocationStateError(
                f"allocation {allocation.ref} is denominated in {allocation.unit!r} "
                f"but the supply is now {supply.unit!r}"
            )
        if allocation.status is not AllocationStatus.CONFIRMED:
            raise AllocationStateError(
                f"allocation {allocation.ref} cannot receive a delivery from "
                f"{allocation.status.value}"
            )
        quantity = quantity.quantize(_QUANTUM, rounding=ROUND_DOWN)
        if quantity <= 0:
            raise AllocationStateError(
                "delivery quantity must be at least 0.001 in canonical resolution"
            )
        outstanding = allocation.quantity - allocation.delivered_quantity
        if quantity > outstanding:
            raise AllocationStateError(
                f"delivery {quantity} exceeds outstanding {outstanding}"
            )
        if supply.on_hand < quantity:
            raise AvailabilityError("on-hand stock cannot cover this delivery")
        now = self._clock()
        allocation.delivered_quantity = (
            allocation.delivered_quantity + quantity
        ).quantize(_QUANTUM, rounding=ROUND_DOWN)
        if allocation.delivered_quantity >= allocation.quantity:
            allocation.status = AllocationStatus.DELIVERED
        allocation.updated_at = now
        supply.on_hand = (supply.on_hand - quantity).quantize(_QUANTUM, rounding=ROUND_DOWN)
        supply.updated_at = now
        await session.flush()
        await record_event(
            session,
            incident_id=need.incident_id,
            event_type="allocation.delivered",
            actor_id=actor_id,
            aggregate_type="allocation",
            aggregate_id=allocation.id,
            payload={
                "allocation_ref": allocation.ref,
                "need_ref": need.ref,
                "quantity": str(quantity),
                "delivered_total": str(allocation.delivered_quantity),
            },
        )
        await self.refresh_need_status(session, need)
        return allocation

    # -- status maintenance -----------------------------------------------------------

    async def refresh_need_status(self, session: AsyncSession, need: Need) -> Need:
        """Recompute a need's status from its committed allocations."""
        if need.status is NeedStatus.CANCELLED:
            return need
        quantities = _quantities(await self.allocations_for_need(session, need.id))
        need.status = derive_need_status(
            quantity=need.quantity,
            delivered_total=delivered_total(quantities),
            reserved_remaining=reserved_remaining(quantities),
            cancelled=False,
        )
        need.updated_at = self._clock()
        await session.flush()
        return need

    # -- locking helpers -----------------------------------------------------------------

    async def _locked_need(self, session: AsyncSession, need_id: UUID) -> Need:
        need = await session.scalar(
            sa.select(Need).where(Need.id == need_id).with_for_update()
        )
        if need is None:
            raise AllocationStateError(f"need {need_id} does not exist")
        return need

    async def _locked_trio(
        self, session: AsyncSession, allocation_id: UUID
    ) -> tuple[Need, Supply, Allocation]:
        peek = await session.get(Allocation, allocation_id)
        if peek is None:
            raise AllocationStateError(f"allocation {allocation_id} does not exist")
        need = await self._locked_need(session, peek.need_id)
        supply = await session.scalar(
            sa.select(Supply).where(Supply.id == peek.supply_id).with_for_update()
        )
        allocation = await session.scalar(
            sa.select(Allocation)
            .where(Allocation.id == allocation_id)
            .with_for_update()
        )
        if supply is None or allocation is None:  # pragma: no cover - FK guarantees
            raise AllocationStateError("allocation aggregate is incomplete")
        return need, supply, allocation

    async def _record_adjusted(
        self,
        session: AsyncSession,
        need: Need,
        draft: AllocationDraft,
        *,
        committed: Decimal,
        reason: str,
        actor_id: UUID | None,
    ) -> None:
        """Audit a draft that commit could not honour exactly as approved."""
        await record_event(
            session,
            incident_id=need.incident_id,
            event_type="allocation.adjusted",
            actor_id=actor_id,
            aggregate_type="need",
            aggregate_id=need.id,
            payload={
                "need_ref": need.ref,
                "supply_id": str(draft.supply_id),
                "approved_quantity": str(draft.quantity),
                "committed_quantity": str(committed),
                "reason": reason,
            },
        )

    async def _cancel_row(
        self,
        session: AsyncSession,
        allocation: Allocation,
        *,
        reason: str,
        actor_id: UUID | None,
    ) -> None:
        allocation.status = AllocationStatus.CANCELLED
        allocation.updated_at = self._clock()
        await session.flush()
        await record_event(
            session,
            incident_id=allocation.incident_id,
            event_type="allocation.cancelled",
            actor_id=actor_id,
            aggregate_type="allocation",
            aggregate_id=allocation.id,
            payload={
                "allocation_ref": allocation.ref,
                "need_id": str(allocation.need_id),
                "reason": reason,
            },
        )

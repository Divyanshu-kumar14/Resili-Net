"""Scoped, single-use action grants and their authorized execution.

A grant is a bearer capability for one participant action (for example
confirming one allocation). Only the SHA-256 hash of the token is stored; the
plaintext exists once, inside the private email that carries the link.

Execution is always inside the caller's transaction and re-checks, under a row
lock, that the grant is live, belongs to the signed-in sender and incident, and
that the actor holds the incident-scoped role or ownership required for the
target. A pending email action additionally requires an intact payload hash, so
neither a forwarded link nor a modified form can change what will happen.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.quantities import QuantityError, canonical_unit, to_canonical, to_stated
from ..models import (
    ActionGrant,
    Allocation,
    Entity,
    Job,
    JobStatus,
    JobType,
    Message,
    Need,
    OutboundEmail,
    OutboundStatus,
    ParticipantRole,
    ParticipantRoleKind,
    PendingAction,
    PendingActionKind,
    PendingActionStatus,
    Report,
    ReviewCase,
    ReviewCaseKind,
    ReviewStatus,
    Sender,
    Supply,
)
from ..schemas import (
    AllocationStatus,
    NeedStatus,
    ParsedEmail,
    ParsedReport,
    ReportKind,
    Resource,
)
from ..security import Principal, hash_token, new_opaque_token
from .allocations import (
    ACTIVE_ALLOCATION_STATUSES,
    AllocationService,
    AllocationStateError,
    AvailabilityError,
)
from .events import record_event
from .notifications import (
    NotificationService,
    OutboxService,
    reply_subject,
)
from .reviews import ReviewService

DEFAULT_GRANT_TTL = timedelta(minutes=30)


class ActionError(Exception):
    """Base class for action failures safe to show as a generic page."""


class ActionInvalid(ActionError):
    """Unknown, expired, consumed, or malformed grant."""


class ActionDenied(ActionError):
    """The signed-in sender is not authorized for this target."""


class ActionStateError(ActionError):
    """The target cannot accept this action right now; the form is unchanged."""


@dataclass(frozen=True, slots=True)
class ActionView:
    kind: PendingActionKind
    title: str
    summary: str
    details: tuple[tuple[str, str], ...]
    submit_label: str


@dataclass(frozen=True, slots=True)
class ActionResult:
    title: str
    message: str


def canonical_payload(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(payload: Mapping[str, Any]) -> str:
    return sha256(canonical_payload(payload).encode()).hexdigest()


def canonical_supply_quantity(report: ParsedReport) -> tuple[Decimal, str]:
    """Resolve a SUPPLY line; zero is a valid empty snapshot."""
    if report.quantity is None:
        raise QuantityError("quantity is missing")
    if canonical_unit(report.resource) is not None:
        converted = to_canonical(report.resource, report.quantity, report.unit)
        return converted.amount, converted.unit.value
    stated = to_stated(report.resource, report.quantity, report.unit)
    return stated.amount, stated.unit


def amount_label(amount: Decimal, unit: str) -> str:
    return f"{amount.normalize():f} {unit}"


async def create_action_grant(
    session: AsyncSession,
    *,
    kind: PendingActionKind,
    incident_id: UUID,
    sender_id: UUID,
    entity_id: UUID | None = None,
    need_id: UUID | None = None,
    allocation_id: UUID | None = None,
    pending_action_id: UUID | None = None,
    payload_hash: str | None = None,
    now: datetime,
    ttl: timedelta = DEFAULT_GRANT_TTL,
) -> tuple[ActionGrant, str]:
    """Create a grant inside the caller's transaction.

    Returns the row and the plaintext token, which the caller must deliver
    privately (never log it, never put it in a public response).
    """
    token = new_opaque_token()
    grant = ActionGrant(
        id=uuid4(),
        kind=kind,
        incident_id=incident_id,
        sender_id=sender_id,
        entity_id=entity_id,
        need_id=need_id,
        allocation_id=allocation_id,
        pending_action_id=pending_action_id,
        payload_hash=payload_hash,
        token_hash=hash_token(token),
        expires_at=now + ttl,
        consumed_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(grant)
    await session.flush()
    return grant, token


class ActionService:
    """Authorize and execute one grant; routes render the result."""

    def __init__(
        self,
        allocations: AllocationService,
        notifications: NotificationService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._allocations = allocations
        self._notifications = notifications
        self._clock = clock or (lambda: datetime.now(UTC))

    async def load(self, session: AsyncSession, token: str) -> ActionGrant:
        grant = await session.scalar(
            sa.select(ActionGrant).where(ActionGrant.token_hash == hash_token(token))
        )
        if grant is None:
            raise ActionInvalid("unknown action link")
        return grant

    def is_live(self, grant: ActionGrant) -> bool:
        return grant.consumed_at is None and grant.expires_at > self._clock()

    async def authorize(
        self, session: AsyncSession, grant: ActionGrant, principal: Principal
    ) -> None:
        """Check sender, incident, target ownership, role, and requester trust."""
        if not self.is_live(grant):
            raise ActionInvalid("action link is no longer usable")
        if grant.sender_id != principal.sender_id:
            raise ActionDenied("this link belongs to another verified sender")
        if grant.incident_id != principal.incident_id:
            raise ActionDenied("this link belongs to another incident")

        now = self._clock()
        if grant.kind in (PendingActionKind.CONFIRM_ALLOCATION, PendingActionKind.UPDATE_SUPPLY):
            entity_id = grant.entity_id
            if grant.kind is PendingActionKind.CONFIRM_ALLOCATION:
                confirmed_allocation, confirmed_need, supply = await self._trio(
                    session, grant
                )
                if confirmed_allocation.status is not AllocationStatus.PROPOSED:
                    raise ActionInvalid("allocation is no longer confirmable")
                entity_id = supply.entity_id
                if grant.entity_id is not None and grant.entity_id != supply.entity_id:
                    raise ActionDenied("grant target does not match the supply depot")
            else:
                pending = await self._pending(session, grant)
                if pending.entity_id is None:
                    raise ActionDenied("stock action has no target entity")
                entity_id = pending.entity_id
                if grant.entity_id is not None and grant.entity_id != entity_id:
                    raise ActionDenied("stock action entity does not match its grant")
            if not (
                await self._has_role(
                    session, principal, ParticipantRoleKind.DEPOT_COORDINATOR, entity_id
                )
                or await self._has_role(session, principal, ParticipantRoleKind.OPERATOR)
            ):
                raise ActionDenied("depot coordinator or operator role required")
            if grant.kind is PendingActionKind.CONFIRM_ALLOCATION:
                await self._require_requester_trust(session, confirmed_need, now)
            return

        allocation: Allocation | None = None
        need: Need | None = None
        if grant.kind is PendingActionKind.CANCEL_NEED:
            need = await self._need(session, grant)
            if need.status in (NeedStatus.CANCELLED, NeedStatus.FULFILLED):
                raise ActionInvalid("need is no longer cancelable")
        else:
            allocation, need, _ = await self._trio(session, grant)
            if grant.kind is PendingActionKind.DELIVER_ALLOCATION:
                if allocation.status is not AllocationStatus.CONFIRMED:
                    raise ActionInvalid("allocation is not waiting for a delivery")
            elif allocation.status not in (
                AllocationStatus.PROPOSED,
                AllocationStatus.CONFIRMED,
            ):
                raise ActionInvalid("allocation is no longer cancelable")

        requester_id = await self._requester_id(session, need)
        allowed = False
        if (requester_id is not None and principal.sender_id == requester_id) or await self._has_role(
            session,
            principal,
            ParticipantRoleKind.ENTITY_COORDINATOR,
            need.entity_id,
        ) or await self._has_role(session, principal, ParticipantRoleKind.OPERATOR):
            allowed = True
        if not allowed:
            raise ActionDenied(
                "only the original requester, the entity coordinator, or an "
                "operator may perform this action"
            )
        if allocation is not None and allocation.incident_id != grant.incident_id:
            raise ActionDenied("allocation belongs to another incident")

    async def view(
        self, session: AsyncSession, grant: ActionGrant
    ) -> ActionView:
        if grant.kind is PendingActionKind.CONFIRM_ALLOCATION:
            allocation, need, supply = await self._trio(session, grant)
            depot = await session.get(Entity, supply.entity_id)
            shelter = await session.get(Entity, need.entity_id)
            return ActionView(
                kind=grant.kind,
                title=f"Confirm allocation {allocation.ref}",
                summary=(
                    "Confirming commits the reserved stock to this need. It is "
                    "not a shipment or a delivery; goods move only when a "
                    "delivery is explicitly reported."
                ),
                details=(
                    ("Need", need.ref),
                    ("Shelter", shelter.name if shelter else "the requesting entity"),
                    ("Depot", depot.name if depot else "the supplying depot"),
                    (
                        "Quantity",
                        amount_label(allocation.quantity, allocation.unit),
                    ),
                    ("Resource", need.resource.value.replace("_", " ")),
                ),
                submit_label="Confirm this allocation",
            )

        pending = await self._pending(session, grant)
        payload = pending.payload_json
        if grant.kind is PendingActionKind.UPDATE_SUPPLY:
            try:
                entity = await session.get(Entity, UUID(str(payload["entity_id"])))
                resource = str(payload["resource"]).replace("_", " ")
                quantity = Decimal(str(payload["quantity"]))
                unit = str(payload["unit"])
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise ActionInvalid("stock action payload is malformed") from exc
            return ActionView(
                kind=grant.kind,
                title="Apply on-hand stock snapshot",
                summary=(
                    "This replaces the stored on-hand figure with the stated "
                    "absolute snapshot. It is not an addition and does not "
                    "dispatch or deliver anything."
                ),
                details=(
                    ("Depot", entity.name if entity else "the depot"),
                    ("Resource", resource),
                    ("On hand", amount_label(quantity, unit)),
                ),
                submit_label="Apply this snapshot",
            )

        if grant.kind is PendingActionKind.DELIVER_ALLOCATION:
            allocation, need, _ = await self._trio(session, grant)
            try:
                quantity = Decimal(str(payload["quantity"]))
                unit = str(payload["unit"])
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise ActionInvalid("delivery payload is malformed") from exc
            return ActionView(
                kind=grant.kind,
                title=f"Record delivery for {allocation.ref}",
                summary=(
                    "Only an explicitly reported delivery consumes on-hand "
                    "stock. This records the stated amount exactly once."
                ),
                details=(
                    ("Need", need.ref),
                    ("Allocation", allocation.ref),
                    ("Delivered", amount_label(quantity, unit)),
                    ("Resource", need.resource.value.replace("_", " ")),
                ),
                submit_label="Record this delivery",
            )

        if grant.kind is PendingActionKind.CANCEL_ALLOCATION:
            allocation, need, _ = await self._trio(session, grant)
            remaining = allocation.quantity - allocation.delivered_quantity
            return ActionView(
                kind=grant.kind,
                title=f"Cancel allocation {allocation.ref}",
                summary=(
                    "Cancelling releases the remaining reservation. Delivered "
                    "history is never altered."
                ),
                details=(
                    ("Need", need.ref),
                    ("Allocation", allocation.ref),
                    (
                        "Reserved to release",
                        amount_label(remaining, allocation.unit),
                    ),
                    ("Resource", need.resource.value.replace("_", " ")),
                ),
                submit_label="Cancel this allocation",
            )

        need = await self._need(session, grant)
        allocations = await self._allocations.allocations_for_need(session, need.id)
        reserved = sum(
            (
                allocation.quantity - allocation.delivered_quantity
                for allocation in allocations
                if allocation.status in ACTIVE_ALLOCATION_STATUSES
            ),
            Decimal("0"),
        )
        return ActionView(
            kind=grant.kind,
            title=f"Cancel need {need.ref}",
            summary=(
                "Cancelling the need releases every remaining reservation for "
                "it. Delivered history is never altered."
            ),
            details=(
                ("Need", need.ref),
                ("Resource", need.resource.value.replace("_", " ")),
                ("Reserved to release", amount_label(reserved, need.unit)),
            ),
            submit_label="Cancel this need",
        )

    async def execute(
        self,
        session: AsyncSession,
        *,
        grant_id: UUID,
        principal: Principal,
    ) -> ActionResult:
        """Consume a live grant and its pending action atomically."""
        grant = await session.get(ActionGrant, grant_id, with_for_update=True)
        if grant is None:
            raise ActionInvalid("unknown action link")
        await self.authorize(session, grant, principal)
        now = self._clock()
        # Claim single-use atomically. The row lock is advisory on SQLite; this
        # conditional write is the portable guard, and a failed mutation rolls
        # the claim back with the rest of the transaction.
        claimed = await session.scalar(
            sa.update(ActionGrant)
            .where(
                ActionGrant.id == grant.id,
                ActionGrant.consumed_at.is_(None),
                ActionGrant.expires_at > now,
            )
            .values(consumed_at=now, updated_at=now)
            .returning(ActionGrant.id)
        )
        if claimed is None:
            raise ActionInvalid("action link is no longer usable")
        pending = (
            await self._pending(session, grant)
            if grant.pending_action_id is not None
            else None
        )
        actor_id = principal.sender_id

        if grant.kind is PendingActionKind.CONFIRM_ALLOCATION:
            result = await self._confirm(session, grant, actor_id)
        elif grant.kind is PendingActionKind.DELIVER_ALLOCATION:
            assert pending is not None
            result = await self._deliver(session, grant, pending, actor_id)
        elif grant.kind is PendingActionKind.CANCEL_ALLOCATION:
            result = await self._cancel_allocation(session, grant, actor_id)
        elif grant.kind is PendingActionKind.CANCEL_NEED:
            result = await self._cancel_need(session, grant, actor_id)
        elif grant.kind is PendingActionKind.UPDATE_SUPPLY:
            assert pending is not None
            result = await self._update_supply(session, grant, pending, actor_id)
        else:  # pragma: no cover - enum is closed
            raise ActionInvalid("unsupported action kind")

        grant.consumed_at = now
        grant.updated_at = now
        if pending is not None:
            pending.status = PendingActionStatus.CONSUMED
            pending.consumed_at = now
            pending.updated_at = now
        await session.flush()
        await record_event(
            session,
            incident_id=grant.incident_id,
            event_type="action.consumed",
            actor_type="sender",
            actor_id=actor_id,
            aggregate_type="action_grant",
            aggregate_id=grant.id,
            payload={
                "action_grant_id": str(grant.id),
                "kind": grant.kind.value,
                "pending_action_id": (
                    str(grant.pending_action_id)
                    if grant.pending_action_id is not None
                    else None
                ),
                "allocation_id": (
                    str(grant.allocation_id) if grant.allocation_id is not None else None
                ),
                "need_id": str(grant.need_id) if grant.need_id is not None else None,
            },
        )
        return result

    # -- target loading ---------------------------------------------------

    async def _pending(
        self, session: AsyncSession, grant: ActionGrant
    ) -> PendingAction:
        if grant.pending_action_id is None:
            raise ActionInvalid("grant has no pending action")
        pending = await session.get(
            PendingAction, grant.pending_action_id, with_for_update=True
        )
        if pending is None:
            raise ActionInvalid("pending action no longer exists")
        if pending.incident_id != grant.incident_id:
            raise ActionDenied("pending action belongs to another incident")
        if pending.status is not PendingActionStatus.PENDING:
            raise ActionInvalid("pending action was already used")
        if pending.kind is not grant.kind:
            raise ActionDenied("action kind does not match its grant")
        if pending.sender_id != grant.sender_id:
            raise ActionDenied("pending action belongs to another sender")
        if pending.payload_hash != payload_hash(pending.payload_json):
            raise ActionDenied("pending action payload was altered")
        if grant.payload_hash is not None and grant.payload_hash != pending.payload_hash:
            raise ActionDenied("grant payload hash does not match the pending action")
        for field in ("entity_id", "need_id", "allocation_id"):
            target = getattr(pending, field)
            if target is not None and getattr(grant, field) != target:
                raise ActionDenied(f"grant target {field} does not match the pending action")
        return pending

    async def _trio(
        self, session: AsyncSession, grant: ActionGrant
    ) -> tuple[Allocation, Need, Supply]:
        if grant.allocation_id is None:
            raise ActionInvalid("grant has no allocation target")
        allocation = await session.get(
            Allocation, grant.allocation_id, with_for_update=True
        )
        if allocation is None:
            raise ActionInvalid("allocation no longer exists")
        if allocation.incident_id != grant.incident_id:
            raise ActionDenied("allocation belongs to another incident")
        if grant.need_id is not None and allocation.need_id != grant.need_id:
            raise ActionDenied("allocation does not belong to the grant's need")
        need = await session.get(Need, allocation.need_id)
        supply = await session.get(Supply, allocation.supply_id)
        if need is None or supply is None:  # pragma: no cover - FK guarantees rows
            raise ActionInvalid("allocation target is incomplete")
        if need.incident_id != grant.incident_id or supply.incident_id != grant.incident_id:
            raise ActionDenied("allocation target belongs to another incident")
        return allocation, need, supply

    async def _need(self, session: AsyncSession, grant: ActionGrant) -> Need:
        if grant.need_id is None:
            raise ActionInvalid("grant has no need target")
        need = await session.get(Need, grant.need_id, with_for_update=True)
        if need is None:
            raise ActionInvalid("need no longer exists")
        if need.incident_id != grant.incident_id:
            raise ActionDenied("need belongs to another incident")
        return need

    # -- authorization helpers -------------------------------------------

    async def _requester_id(
        self, session: AsyncSession, need: Need
    ) -> UUID | None:
        if need.source_report_id is None:
            return None
        report = await session.get(Report, need.source_report_id)
        return report.sender_id if report is not None else None

    async def _requester_sender(
        self, session: AsyncSession, need: Need
    ) -> Sender | None:
        requester_id = await self._requester_id(session, need)
        if requester_id is None:
            return None
        return await session.get(Sender, requester_id)

    async def _require_requester_trust(
        self, session: AsyncSession, need: Need, now: datetime
    ) -> None:
        open_medical = await session.scalar(
            sa.select(ReviewCase.id).where(
                ReviewCase.need_id == need.id,
                ReviewCase.status == ReviewStatus.OPEN,
                ReviewCase.kind == ReviewCaseKind.MEDICAL,
            )
        )
        if open_medical is not None:
            raise ActionDenied(
                "an open medical review must be resolved before this "
                "allocation can be confirmed"
            )
        requester_id = await self._requester_id(session, need)
        requester = await session.get(Sender, requester_id) if requester_id else None
        if requester is not None and requester.verified:
            return
        approval = await session.scalar(
            sa.select(ReviewCase.id).where(
                ReviewCase.need_id == need.id,
                ReviewCase.status == ReviewStatus.RESOLVED,
                ReviewCase.resolution.like("APPROVE_PROPOSAL%"),
            )
        )
        if approval is not None:
            return
        raise ActionDenied(
            "the requesting sender must verify their address or an operator "
            "must record approval before this allocation can be confirmed"
        )

    async def _has_role(
        self,
        session: AsyncSession,
        principal: Principal,
        role: ParticipantRoleKind,
        entity_id: UUID | None = None,
    ) -> bool:
        query = sa.select(ParticipantRole.id).where(
            ParticipantRole.incident_id == principal.incident_id,
            ParticipantRole.sender_id == principal.sender_id,
            ParticipantRole.role == role,
        )
        if role is ParticipantRoleKind.OPERATOR:
            query = query.where(ParticipantRole.entity_id.is_(None))
        elif entity_id is None:
            return False
        else:
            query = query.where(ParticipantRole.entity_id == entity_id)
        return await session.scalar(query.limit(1)) is not None

    # -- mutations --------------------------------------------------------

    async def _confirm(
        self, session: AsyncSession, grant: ActionGrant, actor_id: UUID
    ) -> ActionResult:
        allocation, need, _supply = await self._trio(session, grant)
        if allocation.status is not AllocationStatus.PROPOSED:
            raise ActionStateError(
                f"allocation {allocation.ref} cannot be confirmed from "
                f"{allocation.status.value}"
            )
        try:
            await self._allocations.confirm_allocation(
                session, allocation_id=allocation.id, actor_id=actor_id
            )
        except (AllocationStateError, AvailabilityError) as exc:
            raise ActionStateError(str(exc)) from exc
        requester = await self._requester_sender(session, need)
        if requester is not None:
            await self._enqueue(
                session,
                grant,
                recipient=requester,
                subject=f"Allocation {allocation.ref} confirmed",
                body=self._notifications.confirm_completed_text(
                    allocation_ref=allocation.ref,
                    need_ref=need.ref,
                    quantity_label=amount_label(allocation.quantity, allocation.unit),
                    resource_label=need.resource.value.replace("_", " "),
                ),
                kind=f"action_result:{grant.id}",
            )
        return ActionResult(
            title="Allocation confirmed",
            message=self._notifications.confirm_completed_text(
                allocation_ref=allocation.ref,
                need_ref=need.ref,
                quantity_label=amount_label(allocation.quantity, allocation.unit),
                resource_label=need.resource.value.replace("_", " "),
            ),
        )

    async def _deliver(
        self,
        session: AsyncSession,
        grant: ActionGrant,
        pending: PendingAction,
        actor_id: UUID,
    ) -> ActionResult:
        allocation, need, _ = await self._trio(session, grant)
        quantity = _payload_quantity(pending.payload_json)
        try:
            await self._allocations.record_delivery(
                session,
                allocation_id=allocation.id,
                quantity=quantity,
                actor_id=actor_id,
            )
        except (AllocationStateError, AvailabilityError) as exc:
            raise ActionStateError(str(exc)) from exc
        requester = await self._requester_sender(session, need)
        message = self._notifications.delivery_recorded_text(
            allocation_ref=allocation.ref,
            need_ref=need.ref,
            quantity_label=amount_label(quantity, allocation.unit),
            resource_label=need.resource.value.replace("_", " "),
        )
        if requester is not None:
            await self._enqueue(
                session,
                grant,
                recipient=requester,
                subject=f"Delivery recorded for {allocation.ref}",
                body=message,
                kind=f"action_result:{grant.id}",
            )
        return ActionResult(title="Delivery recorded", message=message)

    async def _cancel_allocation(
        self, session: AsyncSession, grant: ActionGrant, actor_id: UUID
    ) -> ActionResult:
        allocation, need, _ = await self._trio(session, grant)
        remaining = allocation.quantity - allocation.delivered_quantity
        try:
            await self._allocations.cancel_allocation(
                session, allocation_id=allocation.id, actor_id=actor_id
            )
        except (AllocationStateError, AvailabilityError) as exc:
            raise ActionStateError(str(exc)) from exc
        message = self._notifications.cancellation_completed_text(
            reference=allocation.ref,
            released_label=amount_label(remaining, allocation.unit),
            resource_label=need.resource.value.replace("_", " "),
        )
        await self._notify_requester(
            session,
            grant,
            need,
            subject=f"Allocation {allocation.ref} cancelled",
            body=message,
        )
        return ActionResult(title="Allocation cancelled", message=message)

    async def _cancel_need(
        self, session: AsyncSession, grant: ActionGrant, actor_id: UUID
    ) -> ActionResult:
        need = await self._need(session, grant)
        allocations = await self._allocations.allocations_for_need(session, need.id)
        reserved = sum(
            (
                allocation.quantity - allocation.delivered_quantity
                for allocation in allocations
                if allocation.status in ACTIVE_ALLOCATION_STATUSES
            ),
            Decimal("0"),
        )
        try:
            await self._allocations.cancel_need(
                session, need_id=need.id, actor_id=actor_id
            )
        except (AllocationStateError, AvailabilityError) as exc:
            raise ActionStateError(str(exc)) from exc
        message = self._notifications.cancellation_completed_text(
            reference=need.ref,
            released_label=amount_label(reserved, need.unit),
            resource_label=need.resource.value.replace("_", " "),
        )
        await self._notify_requester(
            session,
            grant,
            need,
            subject=f"Need {need.ref} cancelled",
            body=message,
        )
        return ActionResult(title="Need cancelled", message=message)

    async def _update_supply(
        self,
        session: AsyncSession,
        grant: ActionGrant,
        pending: PendingAction,
        actor_id: UUID,
    ) -> ActionResult:
        payload = pending.payload_json
        try:
            entity_id = UUID(str(payload["entity_id"]))
            resource = Resource(str(payload["resource"]))
            quantity = _payload_quantity(payload, allow_zero=True)
            unit = str(payload["unit"])
            report_id = (
                UUID(str(payload["report_id"]))
                if payload.get("report_id") is not None
                else None
            )
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise ActionInvalid("stock action payload is malformed") from exc
        if grant.entity_id is not None and grant.entity_id != entity_id:
            raise ActionDenied("stock action entity does not match its grant")
        if pending.entity_id is None or pending.entity_id != entity_id:
            raise ActionDenied("stock action entity does not match its pending action")
        entity = await session.get(Entity, entity_id)
        if entity is None:
            raise ActionInvalid("stock target no longer exists")
        if entity.incident_id != grant.incident_id:
            raise ActionDenied("stock target belongs to another incident")
        try:
            snapshot = await self._allocations.apply_supply_snapshot(
                session,
                incident_id=grant.incident_id,
                entity_id=entity_id,
                resource=resource,
                quantity=quantity,
                unit=unit,
                source_report_id=report_id,
                actor_id=actor_id,
            )
        except (AllocationStateError, AvailabilityError, QuantityError) as exc:
            raise ActionStateError(str(exc)) from exc

        for allocation in snapshot.cancelled:
            affected = await session.get(Need, allocation.need_id)
            if affected is None:
                continue
            await self._notify_requester(
                session,
                grant,
                affected,
                subject=f"Allocation {allocation.ref} cancelled",
                body=self._notifications.allocation_cancelled_text(
                    allocation_ref=allocation.ref, need_ref=affected.ref
                ),
                kind=f"action_cancel_notice:{grant.id}:{allocation.id}",
            )

        message = self._notifications.supply_applied_text(
            entity_name=entity.name,
            quantity_label=amount_label(quantity, unit),
            resource_label=resource.value.replace("_", " "),
        )
        await self._notify_current_sender(
            session,
            grant,
            subject=f"Stock snapshot applied for {entity.name}",
            body=message,
        )
        await self._enqueue_rematch(session, grant, pending, resource)
        return ActionResult(title="Stock snapshot applied", message=message)

    async def _enqueue_rematch(
        self,
        session: AsyncSession,
        grant: ActionGrant,
        pending: PendingAction,
        resource: Resource,
    ) -> None:
        source_message_id = pending.source_message_id
        if source_message_id is None:
            return
        needs = await self._allocations.ranked_open_needs(
            session, grant.incident_id, resource, self._clock()
        )
        if not needs:
            return
        session.add(
            Job(
                id=uuid4(),
                incident_id=grant.incident_id,
                type=JobType.PROCESS_MESSAGE,
                payload={
                    "message_id": str(source_message_id),
                    "rematch_need_ids": [str(need.id) for need in needs],
                },
                status=JobStatus.PENDING,
                dedupe_key=f"rematch:{pending.id}",
                available_at=self._clock(),
            )
        )
        await session.flush()

    # -- outbox helpers ---------------------------------------------------

    async def _notify_requester(
        self,
        session: AsyncSession,
        grant: ActionGrant,
        need: Need,
        *,
        subject: str,
        body: str,
        kind: str | None = None,
    ) -> None:
        requester = await self._requester_sender(session, need)
        if requester is None:
            return
        await self._enqueue(
            session,
            grant,
            recipient=requester,
            subject=subject,
            body=body,
            kind=kind or f"action_result:{grant.id}",
        )

    async def _notify_current_sender(
        self,
        session: AsyncSession,
        grant: ActionGrant,
        *,
        subject: str,
        body: str,
    ) -> None:
        sender = await session.get(Sender, grant.sender_id)
        if sender is None:  # pragma: no cover - FK guarantees the row
            return
        await self._enqueue(
            session,
            grant,
            recipient=sender,
            subject=subject,
            body=body,
            kind=f"action_result:{grant.id}",
        )

    async def _enqueue(
        self,
        session: AsyncSession,
        grant: ActionGrant,
        *,
        recipient: Sender,
        subject: str,
        body: str,
        kind: str,
    ) -> None:
        existing = await session.scalar(
            sa.select(OutboundEmail.id).where(
                OutboundEmail.kind == kind,
                OutboundEmail.status != OutboundStatus.FAILED,
            )
        )
        if existing is not None:
            return
        await OutboxService.enqueue_direct(
            session,
            incident_id=grant.incident_id,
            recipient=recipient,
            mailbox_id=await self._mailbox_id(session, grant),
            subject=subject,
            body=body,
            kind=kind,
            available_at=self._clock(),
        )

    async def _mailbox_id(self, session: AsyncSession, grant: ActionGrant) -> str:
        if grant.pending_action_id is not None:
            pending = await session.get(PendingAction, grant.pending_action_id)
            if pending is not None and pending.source_message_id is not None:
                message = await session.get(Message, pending.source_message_id)
                if message is not None and message.mailbox_id:
                    return message.mailbox_id
        if grant.need_id is not None:
            need = await session.get(Need, grant.need_id)
            if need is not None and need.source_report_id is not None:
                report = await session.get(Report, need.source_report_id)
                if report is not None:
                    message = await session.get(Message, report.message_id)
                    if message is not None and message.mailbox_id:
                        return message.mailbox_id
        return ""


@dataclass(frozen=True, slots=True)
class IntakeOutcome:
    """What one parsed email action produced, for handler notifications."""

    pending_actions: tuple[PendingAction, ...] = ()
    review_cases: tuple[ReviewCase, ...] = ()


class ActionIntakeService:
    """Turn a validated email action into a PendingAction, never a mutation.

    An authorized sender receives one private single-use link. An unauthorized
    sender still gets an immutable pending record for audit, but no token is
    issued: the handler escalates a review case instead.
    """

    def __init__(
        self,
        notifications: NotificationService,
        *,
        public_base_url: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._notifications = notifications
        self._public_base_url = public_base_url.rstrip("/")
        self._clock = clock or (lambda: datetime.now(UTC))

    async def record(
        self,
        session: AsyncSession,
        *,
        message: Message,
        sender: Sender,
        parsed: ParsedEmail,
        entity_id: UUID | None,
        report_ids: list[UUID],
    ) -> IntakeOutcome:
        if parsed.context.kind is ReportKind.SUPPLY:
            return await self._record_supply(
                session,
                message=message,
                sender=sender,
                parsed=parsed,
                entity_id=entity_id,
                report_ids=report_ids,
            )
        if parsed.context.kind is ReportKind.CANCEL:
            return await self._record_cancel(
                session, message=message, sender=sender, parsed=parsed
            )
        if parsed.context.kind is ReportKind.STATUS_UPDATE:
            return await self._record_status(
                session,
                message=message,
                sender=sender,
                parsed=parsed,
                entity_id=entity_id,
                report_ids=report_ids,
            )
        return IntakeOutcome()

    # -- SUPPLY / STATUS_UPDATE with stock lines --------------------------

    async def _record_supply(
        self,
        session: AsyncSession,
        *,
        message: Message,
        sender: Sender,
        parsed: ParsedEmail,
        entity_id: UUID | None,
        report_ids: list[UUID],
    ) -> IntakeOutcome:
        reviews = ReviewService(session)
        cases: list[ReviewCase] = []
        pendings: list[PendingAction] = []
        if entity_id is None:
            cases.append(
                await reviews.escalate(
                    incident_id=message.incident_id,
                    kind=ReviewCaseKind.OTHER,
                    reason="stock update has no resolvable entity",
                    source_message_id=message.id,
                )
            )
            await self._reply_once(
                session,
                message=message,
                sender=sender,
                kind=f"action_unresolved:{message.id}",
                body=self._notifications.human_review_text(
                    "the depot or location could not be resolved"
                ),
            )
            return IntakeOutcome(tuple(pendings), tuple(cases))

        entity = await session.get(Entity, entity_id)
        entity_name = entity.name if entity is not None else "the depot"
        for index, report in enumerate(parsed.reports):
            report_id = report_ids[index] if index < len(report_ids) else None
            try:
                amount, unit = canonical_supply_quantity(report)
            except QuantityError as exc:
                cases.append(
                    await reviews.escalate(
                        incident_id=message.incident_id,
                        kind=ReviewCaseKind.QUANTITY,
                        reason=f"stock quantity could not be accepted: {exc}",
                        source_message_id=message.id,
                        report_id=report_id,
                    )
                )
                continue
            payload = {
                "entity_id": str(entity_id),
                "resource": report.resource.value,
                "quantity": str(amount),
                "unit": unit,
                "report_id": str(report_id) if report_id is not None else None,
            }
            pending = await self._ensure_pending(
                session,
                message=message,
                sender=sender,
                kind=PendingActionKind.UPDATE_SUPPLY,
                payload=payload,
                entity_id=entity_id,
                need_id=None,
                allocation_id=None,
                source_report_id=report_id,
            )
            pendings.append(pending)
            authorized = await self._authorized_for_entity(
                session, message.incident_id, sender.id, entity_id
            )
            if not authorized:
                case = await reviews.escalate(
                    incident_id=message.incident_id,
                    kind=ReviewCaseKind.ROLE_ASSIGNMENT,
                    reason="stock update requires a verified depot coordinator",
                    source_message_id=message.id,
                    payload={
                        "sender_id": str(sender.id),
                        "entity_id": str(entity_id),
                        "pending_action_id": str(pending.id),
                        "required_role": ParticipantRoleKind.DEPOT_COORDINATOR.value,
                    },
                )
                cases.append(case)
                await self._reply_once(
                    session,
                    message=message,
                    sender=sender,
                    kind=f"action_unauthorized:{pending.id}",
                    body=self._notifications.human_review_text(
                        "marking stock requires depot authority assigned by a coordinator"
                    ),
                )
                continue
            await self._ensure_link(
                session,
                message=message,
                sender=sender,
                pending=pending,
                summary=(
                    f"Apply an on-hand stock snapshot for {entity_name}: "
                    f"{amount_label(amount, unit)} of "
                    f"{report.resource.value.replace('_', ' ')}."
                ),
            )
        return IntakeOutcome(tuple(pendings), tuple(cases))

    # -- CANCEL -----------------------------------------------------------

    async def _record_cancel(
        self,
        session: AsyncSession,
        *,
        message: Message,
        sender: Sender,
        parsed: ParsedEmail,
    ) -> IntakeOutcome:
        reviews = ReviewService(session)
        reference = _normalize_reference(parsed.context.reference_id)
        if reference is None:
            case = await reviews.escalate(
                incident_id=message.incident_id,
                kind=ReviewCaseKind.OTHER,
                reason="cancellation has no resolvable reference",
                source_message_id=message.id,
            )
            await self._reply_once(
                session,
                message=message,
                sender=sender,
                kind=f"action_unresolved:{message.id}",
                body=self._notifications.human_review_text(
                    "cancellations must name an RN- or AL- reference"
                ),
            )
            return IntakeOutcome((), (case,))

        if reference.startswith("RN-"):
            need = await session.scalar(
                sa.select(Need).where(
                    Need.incident_id == message.incident_id, Need.ref == reference
                )
            )
            if need is None:
                return await self._unknown_reference(
                    session, reviews, message=message, sender=sender
                )
            payload = {"need_id": str(need.id), "need_ref": need.ref}
            pending = await self._ensure_pending(
                session,
                message=message,
                sender=sender,
                kind=PendingActionKind.CANCEL_NEED,
                payload=payload,
                entity_id=need.entity_id,
                need_id=need.id,
                allocation_id=None,
                source_report_id=need.source_report_id,
            )
            if not await self._authorized_for_need(
                session, message.incident_id, sender.id, need
            ):
                return await self._unauthorized(
                    session, reviews, message=message, sender=sender, pending=pending
                )
            await self._ensure_link(
                session,
                message=message,
                sender=sender,
                pending=pending,
                summary=f"Cancel need {need.ref}.",
            )
            return IntakeOutcome((pending,), ())

        allocation = await session.scalar(
            sa.select(Allocation).where(
                Allocation.incident_id == message.incident_id,
                Allocation.ref == reference,
            )
        )
        if allocation is None:
            return await self._unknown_reference(
                session, reviews, message=message, sender=sender
            )
        need = await session.get(Need, allocation.need_id)
        if need is None:  # pragma: no cover - FK guarantees the row
            return await self._unknown_reference(
                session, reviews, message=message, sender=sender
            )
        payload = {
            "allocation_id": str(allocation.id),
            "allocation_ref": allocation.ref,
        }
        pending = await self._ensure_pending(
            session,
            message=message,
            sender=sender,
            kind=PendingActionKind.CANCEL_ALLOCATION,
            payload=payload,
            entity_id=need.entity_id,
            need_id=need.id,
            allocation_id=allocation.id,
            source_report_id=need.source_report_id,
        )
        if not await self._authorized_for_need(
            session, message.incident_id, sender.id, need
        ):
            return await self._unauthorized(
                session, reviews, message=message, sender=sender, pending=pending
            )
        await self._ensure_link(
            session,
            message=message,
            sender=sender,
            pending=pending,
            summary=f"Cancel allocation {allocation.ref}.",
        )
        return IntakeOutcome((pending,), ())

    # -- STATUS_UPDATE ----------------------------------------------------

    async def _record_status(
        self,
        session: AsyncSession,
        *,
        message: Message,
        sender: Sender,
        parsed: ParsedEmail,
        entity_id: UUID | None,
        report_ids: list[UUID],
    ) -> IntakeOutcome:
        reviews = ReviewService(session)
        reference = _normalize_reference(parsed.context.reference_id)
        if reference is not None and reference.startswith("AL-"):
            allocation = await session.scalar(
                sa.select(Allocation).where(
                    Allocation.incident_id == message.incident_id,
                    Allocation.ref == reference,
                )
            )
            if allocation is None:
                return await self._unknown_reference(
                    session, reviews, message=message, sender=sender
                )
            need = await session.get(Need, allocation.need_id)
            if need is None or not parsed.reports:
                return await self._unknown_reference(
                    session, reviews, message=message, sender=sender
                )
            report = parsed.reports[0]
            try:
                amount, unit = canonical_supply_quantity(report)
            except QuantityError as exc:
                case = await reviews.escalate(
                    incident_id=message.incident_id,
                    kind=ReviewCaseKind.QUANTITY,
                    reason=f"delivery quantity could not be accepted: {exc}",
                    source_message_id=message.id,
                    report_id=(
                        report_ids[0] if report_ids else None
                    ),
                )
                await self._reply_once(
                    session,
                    message=message,
                    sender=sender,
                    kind=f"action_quantity:{message.id}",
                    body=self._notifications.quantity_review_text(
                        resource_labels=[report.resource.value.replace("_", " ")]
                    ),
                )
                return IntakeOutcome((), (case,))
            if amount <= 0:
                case = await reviews.escalate(
                    incident_id=message.incident_id,
                    kind=ReviewCaseKind.QUANTITY,
                    reason="delivery quantity must be positive",
                    source_message_id=message.id,
                )
                return IntakeOutcome((), (case,))
            if unit != allocation.unit:
                case = await reviews.escalate(
                    incident_id=message.incident_id,
                    kind=ReviewCaseKind.QUANTITY,
                    reason="delivery unit does not match the allocation",
                    source_message_id=message.id,
                    need_id=need.id,
                    allocation_id=allocation.id,
                )
                await self._reply_once(
                    session,
                    message=message,
                    sender=sender,
                    kind=f"action_unit:{message.id}",
                    body=self._notifications.human_review_text(
                        "the delivery unit does not match the reserved allocation"
                    ),
                )
                return IntakeOutcome((), (case,))
            payload = {
                "allocation_id": str(allocation.id),
                "allocation_ref": allocation.ref,
                "quantity": str(amount),
                "unit": unit,
            }
            pending = await self._ensure_pending(
                session,
                message=message,
                sender=sender,
                kind=PendingActionKind.DELIVER_ALLOCATION,
                payload=payload,
                entity_id=need.entity_id,
                need_id=need.id,
                allocation_id=allocation.id,
                source_report_id=need.source_report_id,
            )
            if not await self._authorized_for_need(
                session, message.incident_id, sender.id, need
            ):
                return await self._unauthorized(
                    session, reviews, message=message, sender=sender, pending=pending
                )
            await self._ensure_link(
                session,
                message=message,
                sender=sender,
                pending=pending,
                summary=(
                    f"Record a delivery of {amount_label(amount, unit)} for "
                    f"allocation {allocation.ref}."
                ),
            )
            return IntakeOutcome((pending,), ())

        if reference is not None and reference.startswith("RN-"):
            case = await reviews.escalate(
                incident_id=message.incident_id,
                kind=ReviewCaseKind.OTHER,
                reason="status update for a need requires operator revision",
                source_message_id=message.id,
            )
            await self._reply_once(
                session,
                message=message,
                sender=sender,
                kind=f"action_status:{message.id}",
                body=self._notifications.human_review_text(
                    "changes to a need's quantity are reviewed by a coordinator"
                ),
            )
            return IntakeOutcome((), (case,))

        # A depot status update with stock lines follows the supply path.
        if entity_id is not None and all(
            report.quantity is not None for report in parsed.reports
        ):
            return await self._record_supply(
                session,
                message=message,
                sender=sender,
                parsed=parsed,
                entity_id=entity_id,
                report_ids=report_ids,
            )

        await self._reply_once(
            session,
            message=message,
            sender=sender,
            kind=f"action_required:{message.id}",
            body=self._notifications.action_required_text(),
        )
        return IntakeOutcome()

    # -- shared helpers ---------------------------------------------------

    async def reissue_link(
        self, session: AsyncSession, pending_id: UUID
    ) -> None:
        """Issue a private link after an operator grants the missing authority."""
        pending = await session.get(
            PendingAction, pending_id, with_for_update=True
        )
        if pending is None or pending.status is not PendingActionStatus.PENDING:
            return
        if pending.source_message_id is None:
            return
        message = await session.get(Message, pending.source_message_id)
        sender = await session.get(Sender, pending.sender_id)
        if message is None or sender is None:
            return
        await self._ensure_link(
            session,
            message=message,
            sender=sender,
            pending=pending,
            summary=await self._summary_for(session, pending),
        )

    async def _summary_for(
        self, session: AsyncSession, pending: PendingAction
    ) -> str:
        payload = pending.payload_json
        if pending.kind is PendingActionKind.UPDATE_SUPPLY:
            entity = (
                await session.get(Entity, UUID(str(payload["entity_id"])))
                if payload.get("entity_id")
                else None
            )
            return (
                f"Apply an on-hand stock snapshot for "
                f"{entity.name if entity is not None else 'the depot'}: "
                f"{amount_label(Decimal(str(payload['quantity'])), str(payload['unit']))} "
                f"of {str(payload['resource']).replace('_', ' ')}."
            )
        if pending.kind is PendingActionKind.CANCEL_NEED:
            return f"Cancel need {payload.get('need_ref', '')}."
        if pending.kind is PendingActionKind.CANCEL_ALLOCATION:
            return f"Cancel allocation {payload.get('allocation_ref', '')}."
        if pending.kind is PendingActionKind.DELIVER_ALLOCATION:
            return (
                f"Record a delivery of "
                f"{amount_label(Decimal(str(payload['quantity'])), str(payload['unit']))} "
                f"for allocation {payload.get('allocation_ref', '')}."
            )
        return "Review and submit this private action."

    async def _ensure_pending(
        self,
        session: AsyncSession,
        *,
        message: Message,
        sender: Sender,
        kind: PendingActionKind,
        payload: Mapping[str, Any],
        entity_id: UUID | None,
        need_id: UUID | None,
        allocation_id: UUID | None,
        source_report_id: UUID | None,
    ) -> PendingAction:
        digest = payload_hash(payload)
        existing = await session.scalar(
            sa.select(PendingAction).where(
                PendingAction.source_message_id == message.id,
                PendingAction.kind == kind,
                PendingAction.payload_hash == digest,
            )
        )
        if existing is not None:
            return existing
        pending = PendingAction(
            id=uuid4(),
            kind=kind,
            incident_id=message.incident_id,
            sender_id=sender.id,
            source_message_id=message.id,
            source_report_id=source_report_id,
            entity_id=entity_id,
            need_id=need_id,
            allocation_id=allocation_id,
            payload_json=dict(payload),
            payload_hash=digest,
            status=PendingActionStatus.PENDING,
            consumed_at=None,
            created_at=self._clock(),
            updated_at=self._clock(),
        )
        session.add(pending)
        await session.flush()
        return pending

    async def _ensure_link(
        self,
        session: AsyncSession,
        *,
        message: Message,
        sender: Sender,
        pending: PendingAction,
        summary: str,
    ) -> None:
        now = self._clock()
        kind = f"action_request:{pending.id}"
        already = await session.scalar(
            sa.select(OutboundEmail.id).where(
                OutboundEmail.kind == kind,
                OutboundEmail.status != OutboundStatus.FAILED,
            )
        )
        if already is not None:
            return
        live_id = await session.scalar(
            sa.select(ActionGrant.id).where(
                ActionGrant.pending_action_id == pending.id,
                ActionGrant.consumed_at.is_(None),
                ActionGrant.expires_at > now,
            )
        )
        if live_id is not None:
            # A grant exists but its private email never committed (crash
            # between the two writes). Invalidate it so an unreceived token
            # can never be used, then issue a fresh one for this email.
            live = await session.get(ActionGrant, live_id, with_for_update=True)
            if live is not None:
                live.consumed_at = now
                live.updated_at = now
        _grant, token = await create_action_grant(
            session,
            kind=pending.kind,
            incident_id=pending.incident_id,
            sender_id=pending.sender_id,
            entity_id=pending.entity_id,
            need_id=pending.need_id,
            allocation_id=pending.allocation_id,
            pending_action_id=pending.id,
            payload_hash=pending.payload_hash,
            now=now,
        )
        body = self._notifications.action_request_text(
            summary=summary,
            action_url=f"{self._public_base_url}/actions/{token}",
        )
        if not sender.verified:
            body = (
                "Verify your email address first: a separate verification "
                "message is on its way from our identity provider.\n\n" + body
            )
        await OutboxService.enqueue_direct(
            session,
            incident_id=pending.incident_id,
            recipient=sender,
            mailbox_id=message.mailbox_id or "",
            subject="Private action link for your relief report",
            body=body,
            kind=kind,
            available_at=now,
        )
        await record_event(
            session,
            incident_id=pending.incident_id,
            event_type="action.requested",
            actor_type="sender",
            actor_id=sender.id,
            aggregate_type="pending_action",
            aggregate_id=pending.id,
            payload={
                "pending_action_id": str(pending.id),
                "kind": pending.kind.value,
            },
        )

    async def _unauthorized(
        self,
        session: AsyncSession,
        reviews: ReviewService,
        *,
        message: Message,
        sender: Sender,
        pending: PendingAction,
    ) -> IntakeOutcome:
        case = await reviews.escalate(
            incident_id=message.incident_id,
            kind=ReviewCaseKind.OTHER,
            reason="relief action came from an unauthorized sender",
            source_message_id=message.id,
            payload={
                "sender_id": str(sender.id),
                "pending_action_id": str(pending.id),
            },
        )
        await self._reply_once(
            session,
            message=message,
            sender=sender,
            kind=f"action_unauthorized:{pending.id}",
            body=self._notifications.human_review_text(
                "only the original requester, the entity coordinator, or an "
                "operator may change this record"
            ),
        )
        return IntakeOutcome((pending,), (case,))

    async def _unknown_reference(
        self,
        session: AsyncSession,
        reviews: ReviewService,
        *,
        message: Message,
        sender: Sender,
    ) -> IntakeOutcome:
        case = await reviews.escalate(
            incident_id=message.incident_id,
            kind=ReviewCaseKind.OTHER,
            reason="action reference did not resolve in this incident",
            source_message_id=message.id,
        )
        await self._reply_once(
            session,
            message=message,
            sender=sender,
            kind=f"action_unresolved:{message.id}",
            body=self._notifications.human_review_text(
                "the reference was not found for this incident"
            ),
        )
        return IntakeOutcome((), (case,))

    async def _reply_once(
        self,
        session: AsyncSession,
        *,
        message: Message,
        sender: Sender,
        kind: str,
        body: str,
    ) -> None:
        existing = await session.scalar(
            sa.select(OutboundEmail.id).where(
                OutboundEmail.message_id == message.id,
                OutboundEmail.kind == kind,
                OutboundEmail.status != OutboundStatus.FAILED,
            )
        )
        if existing is not None:
            return
        await OutboxService.enqueue_reply(
            session,
            message=message,
            sender=sender,
            subject=reply_subject(message.subject),
            body=body,
            kind=kind,
            available_at=self._clock(),
        )

    async def _authorized_for_entity(
        self,
        session: AsyncSession,
        incident_id: UUID,
        sender_id: UUID,
        entity_id: UUID,
    ) -> bool:
        role = await session.scalar(
            sa.select(ParticipantRole.id).where(
                ParticipantRole.incident_id == incident_id,
                ParticipantRole.sender_id == sender_id,
                ParticipantRole.role == ParticipantRoleKind.DEPOT_COORDINATOR,
                ParticipantRole.entity_id == entity_id,
            )
        )
        if role is not None:
            return True
        return await self._is_operator(session, incident_id, sender_id)

    async def _authorized_for_need(
        self,
        session: AsyncSession,
        incident_id: UUID,
        sender_id: UUID,
        need: Need,
    ) -> bool:
        if need.source_report_id is not None:
            report = await session.get(Report, need.source_report_id)
            if report is not None and report.sender_id == sender_id:
                return True
        entity_role = await session.scalar(
            sa.select(ParticipantRole.id).where(
                ParticipantRole.incident_id == incident_id,
                ParticipantRole.sender_id == sender_id,
                ParticipantRole.role == ParticipantRoleKind.ENTITY_COORDINATOR,
                ParticipantRole.entity_id == need.entity_id,
            )
        )
        if entity_role is not None:
            return True
        return await self._is_operator(session, incident_id, sender_id)

    async def _is_operator(
        self, session: AsyncSession, incident_id: UUID, sender_id: UUID
    ) -> bool:
        role = await session.scalar(
            sa.select(ParticipantRole.id).where(
                ParticipantRole.incident_id == incident_id,
                ParticipantRole.sender_id == sender_id,
                ParticipantRole.role == ParticipantRoleKind.OPERATOR,
                ParticipantRole.entity_id.is_(None),
            )
        )
        return role is not None


def _normalize_reference(value: str | None) -> str | None:
    if not value:
        return None
    token = value.strip().upper().split()[0]
    token = token.replace(" ", "-")
    if token.startswith("RN") and not token.startswith("RN-"):
        token = "RN-" + token[2:].lstrip("-")
    if token.startswith("AL") and not token.startswith("AL-"):
        token = "AL-" + token[2:].lstrip("-")
    return token or None


def _payload_quantity(
    payload: Mapping[str, Any], *, allow_zero: bool = False
) -> Decimal:
    try:
        quantity = Decimal(str(payload["quantity"]))
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise ActionInvalid("action payload has no valid quantity") from exc
    if not quantity.is_finite() or quantity < 0 or (quantity == 0 and not allow_zero):
        raise ActionStateError("quantity must be a positive finite number")
    return quantity


__all__ = [
    "DEFAULT_GRANT_TTL",
    "ActionDenied",
    "ActionError",
    "ActionIntakeService",
    "ActionResult",
    "ActionService",
    "ActionStateError",
    "ActionView",
    "IntakeOutcome",
    "amount_label",
    "canonical_payload",
    "canonical_supply_quantity",
    "create_action_grant",
    "hash_token",
    "payload_hash",
]

"""Minimal operator review queue and transactional resolutions.

Escalation is created inside the pipeline transaction. Resolution is a separate
operator transaction that re-checks the incident-scoped ``OPERATOR`` role,
validates the case kind and payload, and writes one audit event. Approval never
promises stock: it clears the block and schedules the ordinary matching job, so
normal reservation and requester-trust checks still apply.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    Allocation,
    Entity,
    Job,
    JobStatus,
    JobType,
    Need,
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
)
from ..schemas import AllocationStatus, NeedStatus
from .allocations import AllocationService, AllocationStateError, AvailabilityError
from .events import record_event

RESOLUTIONS: frozenset[str] = frozenset(
    {
        "APPROVE_PROPOSAL",
        "REVISE_QUANTITY",
        "REJECT",
        "MARK_DUPLICATE",
        "ASSIGN_DEPOT_COORDINATOR",
    }
)

#: Which resolutions are meaningful for each case kind. Resolving a case
#: must not clear a block without the decision that belongs to that block.
_ALLOWED_RESOLUTIONS: dict[ReviewCaseKind, frozenset[str]] = {
    ReviewCaseKind.MEDICAL: frozenset({"APPROVE_PROPOSAL", "REVISE_QUANTITY"}),
    ReviewCaseKind.QUANTITY: frozenset({"APPROVE_PROPOSAL", "REVISE_QUANTITY"}),
    ReviewCaseKind.DUPLICATE: frozenset({"MARK_DUPLICATE", "APPROVE_PROPOSAL"}),
    ReviewCaseKind.CRITIC: frozenset({"APPROVE_PROPOSAL", "REJECT"}),
    ReviewCaseKind.ROLE_ASSIGNMENT: frozenset(
        {"ASSIGN_DEPOT_COORDINATOR", "REJECT"}
    ),
    ReviewCaseKind.CLARIFICATION: frozenset({"REJECT"}),
    ReviewCaseKind.DEAD_LETTER: frozenset({"REJECT"}),
    ReviewCaseKind.OTHER: frozenset(
        {"APPROVE_PROPOSAL", "REJECT", "ASSIGN_DEPOT_COORDINATOR"}
    ),
}


class ReviewError(Exception):
    """A review resolution cannot be applied."""


class ReviewDenied(ReviewError):
    """The sender is not an incident operator."""


class ReviewInvalid(ReviewError):
    """The case, resolution, or payload is not valid for this operation."""


class ReviewService:
    """Create (or reuse) open review cases; resolve them as an operator."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._clock = clock or (lambda: datetime.now(UTC))
        self._allocations = AllocationService(clock=self._clock)

    async def escalate(
        self,
        *,
        incident_id: UUID,
        kind: ReviewCaseKind,
        reason: str,
        source_message_id: UUID | None = None,
        report_id: UUID | None = None,
        need_id: UUID | None = None,
        allocation_id: UUID | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> ReviewCase:
        """Return the open case for this escalation, creating it if needed.

        Retried jobs must not stack duplicate cases for the same target, so
        an existing open case of the same kind for the same need or source
        message is reused.
        """
        filters = [
            ReviewCase.incident_id == incident_id,
            ReviewCase.kind == kind,
            ReviewCase.status == ReviewStatus.OPEN,
        ]
        if need_id is not None:
            filters.append(ReviewCase.need_id == need_id)
        if source_message_id is not None:
            filters.append(ReviewCase.source_message_id == source_message_id)
        if need_id is not None or source_message_id is not None:
            existing = await self._session.scalar(
                sa.select(ReviewCase).where(*filters)
            )
            if existing is not None:
                return existing

        case = ReviewCase(
            id=uuid4(),
            incident_id=incident_id,
            kind=kind,
            status=ReviewStatus.OPEN,
            source_message_id=source_message_id,
            report_id=report_id,
            need_id=need_id,
            allocation_id=allocation_id,
            reason=reason[:500],
            payload=dict(payload or {}),
        )
        self._session.add(case)
        await self._session.flush()
        await record_event(
            self._session,
            incident_id=incident_id,
            event_type="review.escalated",
            aggregate_type="review_case",
            aggregate_id=case.id,
            payload={
                "review_case_id": str(case.id),
                "kind": kind.value,
                "need_id": str(need_id) if need_id else None,
            },
        )
        return case

    async def list_open(
        self, *, incident_id: UUID
    ) -> list[ReviewCase]:
        return list(
            await self._session.scalars(
                sa.select(ReviewCase)
                .where(
                    ReviewCase.incident_id == incident_id,
                    ReviewCase.status == ReviewStatus.OPEN,
                )
                .order_by(ReviewCase.created_at, ReviewCase.id)
            )
        )

    async def get_case(
        self, *, incident_id: UUID, case_id: UUID
    ) -> ReviewCase:
        case = await self._session.get(ReviewCase, case_id)
        if case is None or case.incident_id != incident_id:
            raise ReviewInvalid("review case was not found for this incident")
        return case

    async def resolve(
        self,
        *,
        case_id: UUID,
        incident_id: UUID,
        operator_id: UUID,
        resolution: str,
        quantity: Decimal | None = None,
    ) -> ReviewCase:
        """Resolve one open case and write its audit event atomically."""
        case = await self._session.get(ReviewCase, case_id, with_for_update=True)
        if case is None or case.incident_id != incident_id:
            raise ReviewInvalid("review case was not found for this incident")
        if case.status is not ReviewStatus.OPEN:
            raise ReviewInvalid("review case is already resolved")
        if resolution not in RESOLUTIONS:
            raise ReviewInvalid("unsupported review resolution")
        allowed = _ALLOWED_RESOLUTIONS.get(case.kind, frozenset())
        if resolution not in allowed:
            raise ReviewInvalid(
                f"{resolution} is not valid for a {case.kind.value} case"
            )
        await self._require_operator(incident_id, operator_id)
        now = self._clock()
        # Claim the case atomically; a concurrent resolver either saw OPEN
        # before this transaction or loses this conditional write. Any later
        # failure rolls the claim back with the same transaction.
        claimed = await self._session.scalar(
            sa.update(ReviewCase)
            .where(
                ReviewCase.id == case.id,
                ReviewCase.status == ReviewStatus.OPEN,
            )
            .values(status=ReviewStatus.RESOLVED, updated_at=now)
            .returning(ReviewCase.id)
        )
        if claimed is None:
            raise ReviewInvalid("review case is already resolved")

        if resolution == "APPROVE_PROPOSAL":
            await self._approve(case, operator_id)
        elif resolution == "REVISE_QUANTITY":
            await self._revise(case, operator_id, quantity)
        elif resolution == "REJECT":
            await self._reject(case, operator_id)
        elif resolution == "MARK_DUPLICATE":
            await self._mark_duplicate(case, operator_id)
        else:
            await self._assign_depot_coordinator(case, operator_id)

        case.status = ReviewStatus.RESOLVED
        case.resolution = resolution
        case.resolved_by_sender_id = operator_id
        case.resolved_at = now
        case.updated_at = now
        await self._session.flush()
        need_ref: str | None = None
        allocation_ref: str | None = None
        if case.need_id is not None:
            need = await self._session.get(Need, case.need_id)
            need_ref = need.ref if need is not None else None
        if case.allocation_id is not None:
            allocation = await self._session.get(Allocation, case.allocation_id)
            allocation_ref = allocation.ref if allocation is not None else None
        await record_event(
            self._session,
            incident_id=incident_id,
            event_type="review.resolved",
            actor_type="sender",
            actor_id=operator_id,
            aggregate_type="review_case",
            aggregate_id=case.id,
            payload={
                "review_case_id": str(case.id),
                "case_kind": case.kind.value,
                "resolution": resolution,
                "need_ref": need_ref,
                "allocation_ref": allocation_ref,
                "quantity": str(quantity) if quantity is not None else None,
            },
        )
        return case

    async def _require_operator(self, incident_id: UUID, operator_id: UUID) -> None:
        role = await self._session.scalar(
            sa.select(ParticipantRole.id).where(
                ParticipantRole.incident_id == incident_id,
                ParticipantRole.sender_id == operator_id,
                ParticipantRole.role == ParticipantRoleKind.OPERATOR,
                ParticipantRole.entity_id.is_(None),
            )
        )
        if role is None:
            raise ReviewDenied("incident operator role required")

    async def _approve(self, case: ReviewCase, operator_id: UUID) -> None:
        need = await self._locked_need(case)
        if need.needs_human_review:
            need.needs_human_review = False
            need.updated_at = self._clock()
        await self._enqueue_rematch(case, need)
        await record_event(
            self._session,
            incident_id=case.incident_id,
            event_type="need.approved",
            actor_type="sender",
            actor_id=operator_id,
            aggregate_type="need",
            aggregate_id=need.id,
            payload={"need_ref": need.ref, "review_case_id": str(case.id)},
        )

    async def _revise(
        self, case: ReviewCase, operator_id: UUID, quantity: Decimal | None
    ) -> None:
        need = await self._locked_need(case)
        if quantity is None:
            raise ReviewInvalid("a revised quantity is required")
        try:
            parsed = Decimal(quantity)
        except (InvalidOperation, TypeError) as exc:
            raise ReviewInvalid("the revised quantity is not a number") from exc
        if not parsed.is_finite() or parsed <= 0:
            raise ReviewInvalid("the revised quantity must be positive")
        try:
            await self._allocations.update_need(
                self._session,
                need_id=need.id,
                new_quantity=parsed,
                actor_id=operator_id,
            )
        except (AvailabilityError, AllocationStateError) as exc:
            raise ReviewInvalid(str(exc)) from exc
        await self._enqueue_rematch(case, need)

    async def _reject(self, case: ReviewCase, operator_id: UUID) -> None:
        if case.allocation_id is not None:
            allocation = await self._session.get(
                Allocation, case.allocation_id, with_for_update=True
            )
            if allocation is not None and allocation.status is AllocationStatus.PROPOSED:
                allocation.status = AllocationStatus.REJECTED
                allocation.updated_at = self._clock()
                need = await self._session.get(Need, allocation.need_id)
                if need is not None:
                    await self._allocations.refresh_need_status(self._session, need)
                await record_event(
                    self._session,
                    incident_id=case.incident_id,
                    event_type="allocation.rejected",
                    actor_type="sender",
                    actor_id=operator_id,
                    aggregate_type="allocation",
                    aggregate_id=allocation.id,
                    payload={
                        "allocation_ref": allocation.ref,
                        "review_case_id": str(case.id),
                    },
                )
        if case.kind is ReviewCaseKind.ROLE_ASSIGNMENT:
            await self._reject_linked_pending(case, operator_id)

    async def _reject_linked_pending(
        self, case: ReviewCase, operator_id: UUID
    ) -> None:
        pending_id = case.payload.get("pending_action_id")
        if not isinstance(pending_id, str):
            return
        try:
            pending_uuid = UUID(pending_id)
        except ValueError as exc:
            raise ReviewInvalid("linked pending action is malformed") from exc
        pending = await self._session.get(
            PendingAction, pending_uuid, with_for_update=True
        )
        if pending is None or pending.incident_id != case.incident_id:
            raise ReviewInvalid("linked pending action is not in this incident")
        if pending.status is PendingActionStatus.PENDING:
            pending.status = PendingActionStatus.REJECTED
            pending.updated_at = self._clock()
            await self._session.flush()
            await record_event(
                self._session,
                incident_id=case.incident_id,
                event_type="action.rejected",
                actor_type="sender",
                actor_id=operator_id,
                aggregate_type="pending_action",
                aggregate_id=pending.id,
                payload={
                    "pending_action_id": str(pending.id),
                    "kind": pending.kind.value,
                    "review_case_id": str(case.id),
                },
            )

    async def _mark_duplicate(self, case: ReviewCase, operator_id: UUID) -> None:
        if case.kind is not ReviewCaseKind.DUPLICATE:
            raise ReviewInvalid("only duplicate cases can be marked duplicate")
        need = await self._locked_need(case)
        existing_id = case.payload.get("existing_need_id")
        if not isinstance(existing_id, str):
            raise ReviewInvalid("duplicate case has no existing need")
        try:
            existing = await self._session.get(Need, UUID(existing_id))
        except ValueError as exc:
            raise ReviewInvalid("the existing need identifier is malformed") from exc
        if existing is None or existing.incident_id != case.incident_id:
            raise ReviewInvalid("the existing need was not found")
        # Corroboration is preserved: quantities and both needs stay intact.
        await record_event(
            self._session,
            incident_id=case.incident_id,
            event_type="need.duplicate_marked",
            actor_type="sender",
            actor_id=operator_id,
            aggregate_type="need",
            aggregate_id=need.id,
            payload={
                "need_ref": need.ref,
                "existing_need_ref": existing.ref,
                "review_case_id": str(case.id),
            },
        )

    async def _assign_depot_coordinator(
        self, case: ReviewCase, operator_id: UUID
    ) -> None:
        sender_id = case.payload.get("sender_id")
        entity_id = case.payload.get("entity_id")
        if not isinstance(sender_id, str) or not isinstance(entity_id, str):
            raise ReviewInvalid("role assignment needs a sender and entity")
        try:
            sender_uuid = UUID(sender_id)
            entity_uuid = UUID(entity_id)
        except ValueError as exc:
            raise ReviewInvalid("role assignment identifiers are malformed") from exc
        sender = await self._session.get(Sender, sender_uuid)
        entity = await self._session.get(Entity, entity_uuid)
        if (
            sender is None
            or entity is None
            or sender.incident_id != case.incident_id
            or entity.incident_id != case.incident_id
        ):
            raise ReviewInvalid("role assignment targets another incident")
        pending_id = case.payload.get("pending_action_id")
        if pending_id is not None:
            if not isinstance(pending_id, str):
                raise ReviewInvalid("linked pending action is malformed")
            try:
                pending_uuid = UUID(pending_id)
            except ValueError as exc:
                raise ReviewInvalid("linked pending action is malformed") from exc
            pending = await self._session.get(
                PendingAction, pending_uuid, with_for_update=True
            )
            if (
                pending is None
                or pending.incident_id != case.incident_id
                or pending.sender_id != sender.id
                or pending.entity_id != entity.id
                or pending.kind is not PendingActionKind.UPDATE_SUPPLY
                or pending.status is not PendingActionStatus.PENDING
            ):
                raise ReviewInvalid(
                    "linked pending action does not belong to this sender and entity"
                )
        existing = await self._session.scalar(
            sa.select(ParticipantRole.id).where(
                ParticipantRole.incident_id == case.incident_id,
                ParticipantRole.sender_id == sender.id,
                ParticipantRole.role == ParticipantRoleKind.DEPOT_COORDINATOR,
                ParticipantRole.entity_id == entity.id,
            )
        )
        if existing is None:
            self._session.add(
                ParticipantRole(
                    incident_id=case.incident_id,
                    sender_id=sender.id,
                    role=ParticipantRoleKind.DEPOT_COORDINATOR,
                    entity_id=entity.id,
                )
            )
            await self._session.flush()
        await record_event(
            self._session,
            incident_id=case.incident_id,
            event_type="role.assigned",
            actor_type="sender",
            actor_id=operator_id,
            aggregate_type="participant_role",
            aggregate_id=sender.id,
            payload={
                "sender_id": str(sender.id),
                "entity_id": str(entity.id),
                "role": ParticipantRoleKind.DEPOT_COORDINATOR.value,
                "review_case_id": str(case.id),
            },
        )

    async def _locked_need(self, case: ReviewCase) -> Need:
        if case.need_id is None:
            raise ReviewInvalid("review case has no linked need")
        need = await self._session.get(Need, case.need_id, with_for_update=True)
        if need is None or need.incident_id != case.incident_id:
            raise ReviewInvalid("linked need was not found")
        if need.status in (NeedStatus.CANCELLED, NeedStatus.FULFILLED):
            raise ReviewInvalid(f"need {need.ref} is {need.status.value}")
        return need

    async def _enqueue_rematch(self, case: ReviewCase, need: Need) -> None:
        if need.source_report_id is None:
            return
        report = await self._session.get(Report, need.source_report_id)
        if report is None:
            return
        self._session.add(
            Job(
                id=uuid4(),
                incident_id=case.incident_id,
                type=JobType.PROCESS_MESSAGE,
                payload={
                    "message_id": str(report.message_id),
                    "rematch_need_ids": [str(need.id)],
                },
                status=JobStatus.PENDING,
                dedupe_key=f"review-rematch:{case.id}",
                available_at=self._clock(),
            )
        )
        await self._session.flush()


__all__ = [
    "RESOLUTIONS",
    "ReviewDenied",
    "ReviewError",
    "ReviewInvalid",
    "ReviewService",
]

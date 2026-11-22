"""Plain-language email composition.

Every notification is truthful about state: a proposal is recommended, not
dispatched; an estimate is labelled; and a missing match reserves nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db import utcnow
from ..domain.quantities import Quantity, format_quantity
from ..models import (
    Message,
    OutboundEmail,
    OutboundStatus,
    ReviewCase,
    ReviewCaseKind,
    ReviewStatus,
    Sender,
)
from ..ports import InboxProvider, OutboundDraft, ProviderUnavailableError, RawMessage
from ..schemas import Resource
from .events import record_event

logger = logging.getLogger("resilinet.outbox")


@dataclass(frozen=True, slots=True)
class StockLine:
    resource: Resource
    quantity: Quantity


@dataclass(frozen=True, slots=True)
class RecommendationLine:
    need_ref: str
    entity_name: str
    resource: Resource
    quantity: Quantity
    headcount: int | None
    allocation_ref: str | None = None
    supply_name: str | None = None
    distance_km: Decimal | None = None
    requested_quantity: Quantity | None = None


class NotificationService:
    def compose_recommendation(
        self,
        message: RawMessage,
        lines: list[RecommendationLine],
        *,
        unverified: bool,
    ) -> OutboundDraft:
        body: list[str] = []
        for line in lines:
            resource_label = _resource_label(line.resource)
            quantity_label = format_quantity(line.quantity)
            body.append(f"Thanks for the report — good news, here's where things stand for {line.entity_name}.")
            body.append("")
            if line.allocation_ref and line.supply_name:
                distance = (
                    f" (approx. {line.distance_km.quantize(Decimal('0.1'))} km away)"
                    if line.distance_km is not None
                    else ""
                )
                body.append(f"Reference: {line.need_ref} (allocation {line.allocation_ref})")
                body.append(
                    f"Recommended: {line.supply_name} is recommended to supply "
                    f"{quantity_label} of {resource_label}{distance}."
                )
                if (
                    line.requested_quantity is not None
                    and line.requested_quantity.amount > line.quantity.amount
                ):
                    body.append(
                        f"We could only match part of this one: "
                        f"{quantity_label} of the "
                        f"{format_quantity(line.requested_quantity)} requested; "
                        "the rest stays open and we'll keep trying."
                    )
                body.append(
                    "This is a proposal, not a shipment or a dispatch. A depot "
                    "coordinator must confirm it before stock is committed."
                )
            else:
                body.append(f"Reference: {line.need_ref}")
                body.append(
                    f"No stock is currently reserved for {quantity_label} of "
                    f"{resource_label}. The need stays open and we'll keep matching it."
                )
            if line.quantity.source.value == "estimated":
                body.append(
                    f"That {quantity_label} of {resource_label} is estimated from "
                    f"{line.headcount or 0} people using one-day planning rates, "
                    "so treat it as approximate."
                )
            body.append(
                f'Reply "QUERY STATUS {line.need_ref}" for an update.'
            )
            body.append("")
        if unverified:
            body.append(
                "One housekeeping note: your email address isn't verified yet. "
                "Verifying builds trust, but it doesn't change this recommendation."
            )
        return _reply_to(message, "\n".join(body).strip())

    def compose_stock_update(
        self,
        message: RawMessage,
        entity_name: str,
        lines: list[StockLine],
    ) -> OutboundDraft:
        body = [f"Stock recorded for {entity_name} — thanks for the update."]
        for line in lines:
            body.append(
                f"- {format_quantity(line.quantity)} of {_resource_label(line.resource)} on hand"
            )
        body.append("")
        body.append(
            "This is an absolute on-hand snapshot. Reservations are tracked "
            "separately and do not reduce the reported on-hand figure."
        )
        return _reply_to(message, "\n".join(body))

    def compose_clarification(
        self,
        message: RawMessage,
        fields: list[str],
    ) -> OutboundDraft:
        return _reply_to(message, self.clarification_text(fields))

    def clarification_text(self, fields: list[str]) -> str:
        labels = ", ".join(field.replace("_", " ") for field in fields)
        count = f"{len(fields)} detail" + ("" if len(fields) == 1 else "s")
        return (
            "Thanks for getting in touch — before I can match this, I'm missing "
            f"{count}: {labels}. Just reply with the {labels} and I'll carry on "
            "from here. I won't guess at a location, quantity, or resource — "
            "I'd rather ask than get it wrong."
        )

    def split_text(self) -> str:
        return (
            "Thanks, but your message seems to cover more than one place, shelter, "
            "or kind of request, and I don't want to mix them up — so I haven't "
            "logged anything yet. Please send one message per place, with one "
            "need-or-supply update in each, and include the missing details. "
            "No allocation was created and no stock changed."
        )

    def clarification_merged_text(self) -> str:
        return (
            "Got it, thanks — the missing details were added to your original "
            "report, and it's now ready for matching. This note itself doesn't "
            "reserve or promise anything: it's not a reservation, "
            "confirmation, or delivery."
        )

    def query_text(self) -> str:
        return (
            "Happy to look that up — but status checks are read-only, so nothing "
            "will change. Reply with a line such as:\n\n"
            "QUERY STATUS RN-000000\n\n"
            "A reference code identifies a need (RN-) or one allocation (AL-). "
            "Changing stock, confirming an allocation, reporting a delivery, or "
            "cancelling a need requires a verified single-use action link sent to "
            "an authorized participant; email text alone cannot make that change."
        )

    def action_required_text(self) -> str:
        return (
            "Thanks — I can see you want to change stock or an allocation, but I "
            "can't apply that from an email alone: your message did not change "
            "any stock, allocation, or need. Stock snapshots, confirmations, "
            "delivery reports, and cancellations need a verified single-use "
            "action link sent to an authorized participant. Email text alone is "
            "never applied as a change.\n\n"
            "For a read-only status check, reply with: QUERY STATUS RN-000000"
        )

    def human_review_text(self, reason: str | None = None) -> str:
        detail = f" ({reason})" if reason else ""
        return (
            "Thanks for the extra detail — but one part is still ambiguous"
            f"{detail}, so I can't safely match it yet. A coordinator will review "
            "the report before any stock is reserved or promised. No allocation "
            "was created and no stock changed."
        )

    def deadline_ack_text(self) -> str:
        return (
            "Thanks — your message is with me and still being processed. No need "
            "to send it again; I'll reply on this thread as soon as there's an "
            "update. Nothing is reserved or changed yet."
        )

    def processing_failed_text(self) -> str:
        return (
            "Thanks for writing — but something went wrong on my side and I "
            "couldn't process your message automatically. A coordinator has "
            "been notified and will take a look. You don't need to send it "
            "again, and your message changed no stock."
        )

    def wrong_sender_text(self) -> str:
        return (
            "I couldn't apply this reply to the earlier report because it came "
            "from a different email address — the original request is unchanged. "
            "Only the sender of the original report, or an assigned coordinator, "
            "can fill in its missing details."
        )

    def compose_snapshot_rejected(
        self,
        message: RawMessage,
        entity_name: str,
    ) -> OutboundDraft:
        body = (
            f"I couldn't apply the stock snapshot for {entity_name} because it "
            "would leave existing reservations without stock to back them. Your "
            "previous stock figure still stands — a coordinator needs to review "
            "and reconcile this."
        )
        return _reply_to(message, body)

    # -- durable pipeline texts (Task 12) -------------------------------------

    def confirm_request_text(
        self,
        *,
        allocation_ref: str,
        need_ref: str,
        supply_entity: str,
        need_entity: str,
        quantity_label: str,
        resource_label: str,
        action_url: str,
    ) -> str:
        return (
            f"You coordinate stock for {supply_entity} — a volunteer needs a decision.\n\n"
            f"ResiliNet recommends allocating {quantity_label} of "
            f"{resource_label} to {need_entity} (need {need_ref}, allocation "
            f"{allocation_ref}). This is a recommendation, not a dispatch.\n\n"
            f"To confirm this allocation, open your private link within 30 "
            f"minutes and submit the confirmation form:\n{action_url}\n\n"
            "The link works once and only for you. Confirming commits the "
            "stock; it does not report a delivery. If you did not expect "
            "this message, do not open the link and tell an operator."
        )

    def operator_review_text(self, *, review_url: str) -> str:
        return (
            "A relief report needs a person to look at it before any stock can "
            "be committed.\n\n"
            f"Open the review queue: {review_url}\n\n"
            "Signing in is required. This message contains no action link "
            "and cannot change any stock or allocation."
        )

    def duplicate_corroboration_text(
        self, *, need_ref: str, entity_name: str, resource_label: str
    ) -> str:
        return (
            f"Thanks for the report — it backs up an "
            f"existing open request for {resource_label} (reference "
            f"{need_ref}), so it was recorded as corroboration. The "
            "existing quantity was not changed.\n\n"
            f'Reply "QUERY STATUS {need_ref}" for an update.'
        )

    def quantity_review_text(self, *, resource_labels: list[str]) -> str:
        resources = ", ".join(resource_labels)
        return (
            f"Thanks, but I couldn't accept the quantity for {resources} "
            "as stated, so that part of your report was left out while a "
            "coordinator reviews it. Anything else in your report is handled "
            "separately in its own reply. Quantities are never guessed or "
            "reduced silently — what you wrote is what gets reviewed."
        )

    def medical_review_text(self) -> str:
        return (
            "Thanks for the report. Because it involves medical needs, a "
            "coordinator must review it before any medicine or first aid "
            "can be promised. Nothing is reserved for the medical "
            "items, and this message is not a supply promise. Medical "
            "reports jump the queue and get review priority."
        )

    def medical_corroboration_text(self) -> str:
        return (
            "Thanks — I've attached this medical report to the existing "
            "request. A coordinator must review the medical content before "
            "anything moves. Existing reservations are unchanged and are also "
            "under review; this report doesn't promise new stock, and nothing "
            "was dispatched or delivered."
        )

    def reference_not_found_text(self) -> str:
        return (
            "No need or allocation with that reference exists in this "
            "incident. Nothing was changed and no review was opened. "
            "Double-check the reference (RN- for a need, AL- for an "
            "allocation) and send a new query if needed."
        )

    def allocation_status_text(
        self,
        *,
        allocation_ref: str,
        need_ref: str,
        status_label: str,
        resource_label: str,
        quantity_label: str,
        delivered_label: str,
    ) -> str:
        return (
            f"Read-only status for {allocation_ref} (need {need_ref}):\n\n"
            f"Resource: {resource_label}\n"
            f"Status: {status_label}\n"
            f"Reserved quantity: {quantity_label}\n"
            f"Delivered: {delivered_label}\n\n"
            "This lookup changed nothing, as always. Only the original requester, the "
            "entity coordinator, or an operator may read this reference."
        )

    def allocation_cancelled_text(
        self, *, allocation_ref: str, need_ref: str
    ) -> str:
        return (
            f"An earlier recommendation (allocation {allocation_ref}) for "
            f"need {need_ref} was cancelled because the available stock "
            "changed. The need stays open and will be matched again once "
            "stock is available. No goods were dispatched or delivered — "
            "nothing has moved."
        )

    def confirm_completed_text(
        self,
        *,
        allocation_ref: str,
        need_ref: str,
        quantity_label: str,
        resource_label: str,
    ) -> str:
        return (
            f"Allocation {allocation_ref} for need {need_ref} is now confirmed: "
            f"{quantity_label} of {resource_label}. This commits the reserved "
            "stock to that need. It's not a shipment or a delivery — please "
            "report a delivery separately once the goods actually arrive."
        )

    def delivery_recorded_text(
        self,
        *,
        allocation_ref: str,
        need_ref: str,
        quantity_label: str,
        resource_label: str,
    ) -> str:
        return (
            f"A delivery of {quantity_label} of {resource_label} was recorded "
            f"against allocation {allocation_ref} (need {need_ref}). On-hand "
            "stock was reduced by that amount exactly once. If this is wrong, "
            "tell a coordinator — and please don't send a second delivery report."
        )

    def cancellation_completed_text(
        self, *, reference: str, released_label: str, resource_label: str
    ) -> str:
        return (
            f"{reference} was cancelled. Any remaining reservation for "
            f"{released_label} of {resource_label} was released. Delivered "
            "history is untouched. No goods were dispatched by this action."
        )

    def supply_applied_text(
        self,
        *,
        entity_name: str,
        quantity_label: str,
        resource_label: str,
    ) -> str:
        return (
            f"Done — your on-hand snapshot for {entity_name} was applied: "
            f"{quantity_label} of {resource_label}. A snapshot is an absolute "
            "figure, not an addition. Any reservation the snapshot cannot "
            "support is cancelled newest-first, and affected requesters are "
            "notified. This doesn't dispatch or deliver anything."
        )

    def action_request_text(
        self,
        *,
        summary: str,
        action_url: str,
        expires_minutes: int = 30,
    ) -> str:
        return (
            "ResiliNet got an email action it can't apply directly — email text "
            "can never change stock or an allocation.\n\n"
            f"{summary}\n\n"
            "Review and submit it with your private single-use link:\n"
            f"{action_url}\n\n"
            f"The link expires in {expires_minutes} minutes, works once, and "
            "only for the verified address that received it. If you didn't "
            "send this request, do not open the link and tell an operator."
        )

    def status_text(
        self,
        *,
        need_ref: str,
        resource_label: str,
        status_label: str,
        required_label: str,
        reserved_label: str,
        delivered_label: str,
        allocation_refs: list[str],
    ) -> str:
        allocations = ", ".join(allocation_refs) if allocation_refs else "none"
        return (
            f"Read-only status for {need_ref}:\n\n"
            f"Resource: {resource_label}\n"
            f"Status: {status_label}\n"
            f"Required: {required_label}\n"
            f"Reserved: {reserved_label}\n"
            f"Delivered: {delivered_label}\n"
            f"Allocations: {allocations}\n\n"
            "This lookup changed nothing, as always. Email commands other than QUERY STATUS "
            "are never applied; confirmations, deliveries, cancellations, and "
            "stock changes require a verified single-use action link."
        )


@dataclass(frozen=True, slots=True)
class RecommendationText:
    """One pre-formatted recommendation line for the durable pipeline."""

    need_ref: str
    entity_name: str
    resource_label: str
    quantity_label: str
    allocation_ref: str | None = None
    supply_name: str | None = None
    distance_km: Decimal | None = None
    requested_label: str | None = None
    estimated: bool = False
    headcount: int | None = None


def recommendation_body(lines: list[RecommendationText], *, unverified: bool) -> str:
    """Plain-language recommendation text shared by every reply path."""
    body: list[str] = []
    for line in lines:
        body.append(f"Thanks for the report — good news, here's where things stand for {line.entity_name}.")
        body.append("")
        if line.allocation_ref:
            distance = (
                f" (approx. {line.distance_km.quantize(Decimal('0.1'))} km away)"
                if line.distance_km is not None
                else ""
            )
            supplier = line.supply_name or "The supplying depot"
            body.append(f"Reference: {line.need_ref} (allocation {line.allocation_ref})")
            body.append(
                f"Recommended: {supplier} is recommended to supply "
                f"{line.quantity_label} of {line.resource_label}{distance}."
            )
            if line.requested_label is not None:
                body.append(
                    f"We could only match part of this one: "
                    f"{line.quantity_label} of the {line.requested_label} "
                    "requested, so the rest stays open and we'll keep trying."
                )
            body.append(
                "This is a proposal, not a shipment or a dispatch. A depot "
                "coordinator must confirm it before stock is committed."
            )
        else:
            body.append(f"Reference: {line.need_ref}")
            body.append(
                f"No stock is currently reserved for {line.quantity_label} of "
                f"{line.resource_label}. The need stays open and we'll keep matching it."
            )
        if line.estimated:
            body.append(
                f"That {line.quantity_label} of {line.resource_label} is estimated from "
                f"{line.headcount or 0} people using one-day planning rates, "
                "so treat it as approximate."
            )
        body.append(f'Reply "QUERY STATUS {line.need_ref}" for an update.')
        body.append("")
    if unverified:
        body.append(
            "One housekeeping note: your email address isn't verified yet. "
            "Verifying builds trust, but it doesn't change this recommendation."
        )
    return "\n".join(body).strip()


def stock_body(entity_name: str, display_lines: list[str]) -> str:
    """Stock acknowledgement text for a recorded absolute snapshot."""
    body = [f"Stock recorded for {entity_name} — thanks for the update."]
    body.extend(f"- {line} on hand" for line in display_lines)
    body.append("")
    body.append(
        "This is an absolute on-hand snapshot — the figure you reported, not "
        "a running total. Reservations are tracked separately and don't "
        "reduce the reported on-hand figure."
    )
    return "\n".join(body)


class OutboxService:
    """Durable outbound email queued in the caller's transaction.

    A row is created with a stable ``outbox:<uuid>`` idempotency key, so a retry
    of the same logical notification can never send twice. Delivery happens
    outside database transactions and is claimed with a row lock. A row left in
    ``sending`` by a crash is reclaimed after ``stale_seconds``; the provider
    idempotency key makes that replay safe. SQLite ignores ``SKIP LOCKED``, so
    local development runs one consumer.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        inbox: InboxProvider,
        *,
        clock: Callable[[], datetime] | None = None,
        max_attempts: int = 3,
        retry_base_seconds: float = 5.0,
        stale_seconds: float = 120.0,
    ) -> None:
        self._session_factory = session_factory
        self._inbox = inbox
        self._clock = clock or utcnow
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._stale_seconds = stale_seconds

    @staticmethod
    async def enqueue_reply(
        session: AsyncSession,
        *,
        message: Message,
        sender: Sender,
        subject: str,
        body: str,
        kind: str,
        available_at: datetime | None = None,
    ) -> OutboundEmail:
        """Queue one reply inside the caller's transaction."""
        outbox_id = uuid4()
        row = OutboundEmail(
            id=outbox_id,
            incident_id=message.incident_id,
            message_id=message.id,
            sender_id=sender.id,
            to_address=sender.address,
            subject=subject,
            body_text=body,
            kind=kind,
            idempotency_key=f"outbox:{outbox_id}",
            available_at=available_at or utcnow(),
        )
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    async def enqueue_direct(
        session: AsyncSession,
        *,
        incident_id: UUID,
        recipient: Sender,
        mailbox_id: str,
        subject: str,
        body: str,
        kind: str,
        available_at: datetime | None = None,
    ) -> OutboundEmail:
        """Queue a non-reply email (e.g. a coordinator action request)."""
        outbox_id = uuid4()
        row = OutboundEmail(
            id=outbox_id,
            incident_id=incident_id,
            message_id=None,
            sender_id=recipient.id,
            to_address=recipient.address,
            subject=subject,
            body_text=body,
            kind=kind,
            mailbox_id=mailbox_id,
            idempotency_key=f"outbox:{outbox_id}",
            available_at=available_at or utcnow(),
        )
        session.add(row)
        await session.flush()
        return row

    async def deliver_due(self) -> bool:
        """Deliver the oldest due outbox row. Returns False when none is due.

        A row left in ``sending`` by a crashed worker is reclaimed after
        ``stale_seconds``; the provider idempotency key makes that replay safe.
        """
        now = self._clock()
        stale_before = now - timedelta(seconds=self._stale_seconds)
        async with self._session_factory() as session:
            candidate = await session.scalar(
                sa.select(OutboundEmail.id)
                .where(
                    sa.or_(
                        sa.and_(
                            OutboundEmail.status == OutboundStatus.PENDING,
                            OutboundEmail.available_at <= now,
                        ),
                        sa.and_(
                            OutboundEmail.status == OutboundStatus.SENDING,
                            OutboundEmail.updated_at < stale_before,
                        ),
                    )
                )
                .order_by(OutboundEmail.available_at, OutboundEmail.created_at)
                .limit(1)
            )
        if candidate is None:
            return False
        return await self.deliver_one(candidate)

    async def deliver_one(self, outbox_id: UUID) -> bool:
        claim = await self._claim(outbox_id)
        if claim is None:
            return False
        row, message = claim
        claimed_attempt = row.attempt_count
        draft = OutboundDraft(
            mailbox_id=(
                message.mailbox_id
                if message is not None
                else row.mailbox_id or ""
            ),
            to=row.to_address,
            subject=row.subject,
            text=row.body_text,
            in_reply_to=message.provider_message_id if message is not None else None,
        )
        try:
            if message is not None:
                receipt = await self._inbox.reply(
                    message.provider_message_id, draft, row.idempotency_key
                )
            else:
                receipt = await self._inbox.send(draft, row.idempotency_key)
        except ProviderUnavailableError as exc:
            await self._record_failure(
                row.id,
                retryable=exc.retryable,
                detail=str(exc),
                error_type=type(exc).__name__,
                expected_attempt=claimed_attempt,
            )
            return True
        except Exception as exc:
            await self._record_failure(
                row.id,
                retryable=False,
                detail=str(exc),
                error_type=type(exc).__name__,
                expected_attempt=claimed_attempt,
            )
            return True
        await self._record_sent(
            row.id, receipt.provider_message_id, expected_attempt=claimed_attempt
        )
        return True

    async def _claim(self, outbox_id: UUID) -> tuple[OutboundEmail, Message | None] | None:
        now = self._clock()
        stale_before = now - timedelta(seconds=self._stale_seconds)
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                sa.select(OutboundEmail)
                .where(
                    OutboundEmail.id == outbox_id,
                    sa.or_(
                        OutboundEmail.status == OutboundStatus.PENDING,
                        sa.and_(
                            OutboundEmail.status == OutboundStatus.SENDING,
                            OutboundEmail.updated_at < stale_before,
                        ),
                    ),
                )
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            if (
                row.status is OutboundStatus.SENDING
                and row.attempt_count >= self._max_attempts
            ):
                row.status = OutboundStatus.FAILED
                row.last_error = "delivery lease expired after the final attempt"
                row.updated_at = now
                # Same operator visibility as the exception path: a crashed
                # worker's undelivered email must reach the review queue.
                session.add(
                    ReviewCase(
                        id=uuid4(),
                        incident_id=row.incident_id,
                        kind=ReviewCaseKind.OTHER,
                        status=ReviewStatus.OPEN,
                        source_message_id=row.message_id,
                        reason="outbound email delivery failed permanently",
                        payload={
                            "outbox_id": str(row.id),
                            "kind": row.kind,
                            "error_type": "LeaseExpired",
                        },
                    )
                )
                await session.flush()
                await record_event(
                    session,
                    incident_id=row.incident_id,
                    event_type="outbox.failed",
                    aggregate_type="outbound_email",
                    aggregate_id=row.id,
                    payload={
                        "outbox_id": str(row.id),
                        "kind": row.kind,
                        "attempt": row.attempt_count,
                        "error_type": "LeaseExpired",
                    },
                )
                return None
            row.status = OutboundStatus.SENDING
            row.attempt_count += 1
            row.updated_at = now
            message = (
                await session.get(Message, row.message_id)
                if row.message_id is not None
                else None
            )
            return row, message

    async def _record_sent(
        self,
        outbox_id: UUID,
        provider_message_id: str,
        *,
        expected_attempt: int,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            row = await session.get(
                OutboundEmail, outbox_id, with_for_update=True
            )
            if row is None:  # pragma: no cover - FK guarantees the row
                return
            if (
                row.status is not OutboundStatus.SENDING
                or row.attempt_count != expected_attempt
            ):
                # A late worker lost its lease; never overwrite the state of
                # the worker that reclaimed the row.
                return
            row.status = OutboundStatus.SENT
            row.sent_at = self._clock()
            row.updated_at = self._clock()
            row.provider_message_id = provider_message_id
            row.last_error = None
            await record_event(
                session,
                incident_id=row.incident_id,
                event_type="outbox.sent",
                aggregate_type="outbound_email",
                aggregate_id=row.id,
                payload={"outbox_id": str(row.id), "kind": row.kind},
            )

    async def _record_failure(
        self,
        outbox_id: UUID,
        *,
        retryable: bool,
        detail: str,
        error_type: str,
        expected_attempt: int,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            row = await session.get(
                OutboundEmail, outbox_id, with_for_update=True
            )
            if row is None:  # pragma: no cover - FK guarantees the row
                return
            if (
                row.status is not OutboundStatus.SENDING
                or row.attempt_count != expected_attempt
            ):
                return
            can_retry = retryable and row.attempt_count < self._max_attempts
            row.status = (
                OutboundStatus.PENDING if can_retry else OutboundStatus.FAILED
            )
            row.last_error = detail[:400]
            row.updated_at = self._clock()
            if can_retry:
                delay = self._retry_base_seconds * (2 ** (row.attempt_count - 1))
                row.available_at = self._clock() + timedelta(seconds=delay)
            else:
                # A permanently failed notification becomes an operator
                # review case: the participant impact is visible in the
                # queue instead of disappearing into a log line.
                session.add(
                    ReviewCase(
                        id=uuid4(),
                        incident_id=row.incident_id,
                        kind=ReviewCaseKind.OTHER,
                        status=ReviewStatus.OPEN,
                        source_message_id=row.message_id,
                        reason="outbound email delivery failed permanently",
                        payload={
                            "outbox_id": str(row.id),
                            "kind": row.kind,
                            "error_type": error_type,
                        },
                    )
                )
                await session.flush()
            await record_event(
                session,
                incident_id=row.incident_id,
                event_type=(
                    "outbox.retry_scheduled" if can_retry else "outbox.failed"
                ),
                aggregate_type="outbound_email",
                aggregate_id=row.id,
                payload={
                    "outbox_id": str(row.id),
                    "kind": row.kind,
                    "attempt": row.attempt_count,
                    "error_type": error_type,
                },
            )
        logger.warning(
            "outbox_delivery_failed",
            extra={"outbox_id": str(outbox_id), "retry": can_retry},
        )


def reply_subject(subject: str) -> str:
    # Collapse CR/LF and other whitespace so an inbound subject can never
    # inject a second header into the outbound message.
    clean = " ".join(subject.split()) or "Your ResiliNet report"
    return clean if clean.casefold().startswith("re:") else f"Re: {clean}"


def _resource_label(resource: Resource) -> str:
    return resource.value.replace("_", " ")


def _reply_to(message: RawMessage, body: str) -> OutboundDraft:
    return OutboundDraft(
        mailbox_id=message.mailbox_id,
        to=message.sender,
        subject=reply_subject(message.subject),
        text=body,
        in_reply_to=message.provider_message_id,
    )

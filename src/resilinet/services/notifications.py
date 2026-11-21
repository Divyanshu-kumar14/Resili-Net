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
from ..models import Message, OutboundEmail, OutboundStatus, Sender
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
            body.append(f"We received your report for {line.entity_name}.")
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
                        f"This is a partial match for the "
                        f"{format_quantity(line.requested_quantity)} requested; "
                        "the remainder stays open."
                    )
                body.append(
                    "This is a proposal, not a shipment or a dispatch. A depot "
                    "coordinator must confirm it before stock is committed."
                )
            else:
                body.append(f"Reference: {line.need_ref}")
                body.append(
                    f"No stock is currently reserved for {quantity_label} of "
                    f"{resource_label}. The need remains open for matching."
                )
            if line.quantity.source.value == "estimated":
                body.append(
                    f"Quantity: {quantity_label} of {resource_label}, estimated from "
                    f"{line.headcount or 0} people using planning rates for one day."
                )
            body.append(
                f'Reply "QUERY STATUS {line.need_ref}" for an update.'
            )
            body.append("")
        if unverified:
            body.append(
                "Your sender address is not verified yet. Verification upgrades "
                "trust but does not change this recommendation."
            )
        return _reply_to(message, "\n".join(body).strip())

    def compose_stock_update(
        self,
        message: RawMessage,
        entity_name: str,
        lines: list[StockLine],
    ) -> OutboundDraft:
        body = [f"Stock recorded for {entity_name}."]
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
        return (
            "We received your message but need one more detail before we can match it: "
            f"{labels}. Please reply with the {labels}. We will not guess a location, "
            "quantity, or resource."
        )

    def split_text(self) -> str:
        return (
            "Your message appears to describe more than one place, entity, or kind of "
            "report. We did not guess which item belongs where. Please send one message "
            "per place and per need-or-supply report, and include the missing details. "
            "No allocation was created and no stock changed."
        )

    def clarification_merged_text(self) -> str:
        return (
            "Thank you — the missing details were added to your original report. "
            "The report is now ready for matching. This acknowledgement is not a "
            "reservation, confirmation, or delivery."
        )

    def query_text(self) -> str:
        return (
            "Status queries are read-only. Reply with a line such as:\n\n"
            "QUERY STATUS RN-000000\n\n"
            "A reference code identifies a need (RN-) or one allocation (AL-). "
            "Changing stock, confirming an allocation, reporting a delivery, or "
            "cancelling a need requires a verified single-use action link sent to "
            "an authorized participant; email text alone cannot make that change."
        )

    def action_required_text(self) -> str:
        return (
            "We received your message, but it did not change any stock, allocation, "
            "or need. Stock snapshots, confirmations, delivery reports, and "
            "cancellations require a verified single-use action link sent to an "
            "authorized participant. Email text alone is never applied as a change.\n\n"
            "For a read-only status check, reply with: QUERY STATUS RN-000000"
        )

    def human_review_text(self, reason: str | None = None) -> str:
        detail = f" ({reason})" if reason else ""
        return (
            "We received your reply, but one of the details is still ambiguous"
            f"{detail}. A coordinator will review the report before any stock is "
            "reserved or promised. No allocation was created and no stock changed."
        )

    def wrong_sender_text(self) -> str:
        return (
            "This reply could not be applied to the earlier report because it came "
            "from a different email address. The original request is unchanged. "
            "Only the sender of the original report, or an assigned coordinator, "
            "can supply its missing details."
        )

    def compose_snapshot_rejected(
        self,
        message: RawMessage,
        entity_name: str,
    ) -> OutboundDraft:
        body = (
            f"The stock snapshot for {entity_name} was not applied because it would "
            "leave existing reservations unsupported. The previous stock figure is "
            "unchanged. A coordinator must review and reconcile it."
        )
        return _reply_to(message, body)


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
        draft = OutboundDraft(
            mailbox_id=message.mailbox_id if message is not None else "",
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
                row.id, retryable=exc.retryable, detail=str(exc), error_type=type(exc).__name__
            )
            return True
        except Exception as exc:
            await self._record_failure(
                row.id, retryable=False, detail=str(exc), error_type=type(exc).__name__
            )
            return True
        await self._record_sent(row.id, receipt.provider_message_id)
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

    async def _record_sent(self, outbox_id: UUID, provider_message_id: str) -> None:
        async with self._session_factory() as session, session.begin():
            row = await session.get(OutboundEmail, outbox_id)
            if row is None:  # pragma: no cover - FK guarantees the row
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
        self, outbox_id: UUID, *, retryable: bool, detail: str, error_type: str
    ) -> None:
        async with self._session_factory() as session, session.begin():
            row = await session.get(OutboundEmail, outbox_id)
            if row is None:  # pragma: no cover - FK guarantees the row
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
    clean = subject.strip() or "Your ResiliNet report"
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

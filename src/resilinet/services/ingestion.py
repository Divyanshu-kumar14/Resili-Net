"""Durable inbound ingestion.

Accepting an email is one transaction: normalise the provider event, resolve the
sender, enforce the persisted rolling rate limits, insert the message exactly
once per ``(incident, provider_message_id)``, and enqueue durable work. Nothing
here extracts, geocodes, matches, or mutates stock; those are worker jobs, so an
intake retry can never repeat a domain change.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import (
    Incident,
    Job,
    JobStatus,
    JobType,
    Message,
    MessageStatus,
    OutboundEmail,
    Sender,
)
from ..ports import InboundEvent
from ..security import normalize_email
from .events import record_event

#: Persisted rolling limits from the P0 plan.
SENDER_LIMIT_PER_HOUR = 20
INCIDENT_LIMIT_PER_HOUR = 200

_WINDOW = timedelta(hours=1)

#: A still-unprocessed message earns exactly one acknowledgement after this.
DEADLINE_ACK_DELAY = timedelta(seconds=15)
_SIGNATURE_DELIMITER = re.compile(r"^--\s*$")
_MOBILE_SIGNATURE = re.compile(r"^(sent from|get outlook for)\b", re.IGNORECASE)
_QUOTE_HEADER = re.compile(r"^on\b.+\bwrote:\s*$", re.IGNORECASE)
_MULTIPLE_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)


class IngestionError(RuntimeError):
    """An event cannot be accepted into this incident."""


@dataclass(frozen=True, slots=True)
class AcceptResult:
    duplicate: bool
    message_id: UUID
    throttled: bool = False
    job_id: UUID | None = None


def normalize_address(address: str) -> str:
    """Normalise an email address for identity comparison only."""
    return normalize_email(address)


def normalize_inbound_text(text: str) -> str:
    """Strip quoted replies and signatures, then normalise unicode and spacing.

    The transformation is deliberately conservative: if stripping would leave
    nothing, the normalised original is returned so content is never silently
    lost. Extraction receives this text; the provider's raw text stays in
    ``Message.raw_text``.
    """
    normalized = unicodedata.normalize("NFKC", text.replace("\r\n", "\n").replace("\r", "\n"))
    kept: list[str] = []
    for line in normalized.split("\n"):
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if _SIGNATURE_DELIMITER.match(stripped) or _MOBILE_SIGNATURE.match(stripped):
            break
        if _QUOTE_HEADER.match(stripped):
            break
        kept.append(line.rstrip())
    body = "\n".join(kept)
    body = _MULTIPLE_BLANK_LINES.sub("\n\n", body).strip()
    return body or _collapse_blank_lines(normalized).strip()


def _collapse_blank_lines(text: str) -> str:
    return _TRAILING_SPACE.sub("", _MULTIPLE_BLANK_LINES.sub("\n\n", text))


def _clip(value: str | None, limit: int) -> str:
    text = (value or "").strip()
    return text[:limit]


def normalize_subject(subject: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", subject).split())[:500]


def _throttle_body(limit: int, window_label: str) -> str:
    return (
        "We received your message, but it was not processed because this "
        f"{window_label} has reached the intake limit of {limit} messages per hour. "
        "No report, stock figure, or allocation was changed.\n\n"
        "Please wait before sending another update. If this is an emergency, "
        "contact the incident operator directly."
    )


class IngestionService:
    """The durable intake transaction."""

    def __init__(
        self,
        incident_id: UUID,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
        sender_limit: int = SENDER_LIMIT_PER_HOUR,
        incident_limit: int = INCIDENT_LIMIT_PER_HOUR,
    ) -> None:
        self._incident_id = incident_id
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sender_limit = sender_limit
        self._incident_limit = incident_limit

    async def accept(
        self,
        event: InboundEvent,
        raw_headers: Mapping[str, str] | None = None,
    ) -> AcceptResult:
        """Accept one provider event idempotently.

        A duplicate provider event returns the original message ID and never
        creates a second job. A rate-limited event is recorded for audit, is not
        processed, and queues a sender-visible notice.
        """
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            # Lock the incident row so the persisted rolling limits are
            # evaluated serially on PostgreSQL; without this, two concurrent
            # deliveries could both observe the pre-limit count.
            incident = await session.get(
                Incident, self._incident_id, with_for_update=True
            )
            if incident is None:
                raise IngestionError(f"unknown incident {self._incident_id}")

            provider_message_id = _clip(event.provider_message_id, 200)
            existing = await session.scalar(
                sa.select(Message).where(
                    Message.incident_id == self._incident_id,
                    Message.provider_message_id == provider_message_id,
                )
            )
            if existing is not None:
                return AcceptResult(duplicate=True, message_id=existing.id)

            sender_address = normalize_address(event.sender)
            if not sender_address:
                raise IngestionError("inbound event has no sender address")
            sender = await self._resolve_sender(session, sender_address, now)

            original = event.raw
            raw_text = original.text if original is not None else ""
            body_text = normalize_inbound_text(raw_text) if raw_text else ""
            # A withheld event may still carry a partial body, but the provider
            # is holding the authoritative content: always fetch it first.
            deferred = event.withheld or original is None

            message = Message(
                id=uuid4(),
                incident_id=self._incident_id,
                sender_id=sender.id,
                provider_message_id=provider_message_id,
                mailbox_id=_clip(event.mailbox_id, 200),
                thread_id=_clip(event.thread_id or (original.thread_id if original else None), 200) or None,
                subject=normalize_subject(event.subject),
                body_text=body_text,
                raw_text=raw_text,
                status=MessageStatus.DEFERRED if deferred else MessageStatus.QUEUED,
                withheld=event.withheld,
                attachment_count=original.attachment_count if original else 0,
                signature_verification={
                    # The provider exposes no documented SPF/DKIM/DMARC fields;
                    # recording "unknown" is honest, guessing would not be.
                    "spf": "unknown",
                    "dkim": "unknown",
                    "dmarc": "unknown",
                },
                received_at=event.received_at,
                created_at=now,
                updated_at=now,
            )
            try:
                async with session.begin_nested():
                    session.add(message)
                    await session.flush()
            except IntegrityError:
                # A concurrent delivery won the unique constraint.
                raced = await session.scalar(
                    sa.select(Message).where(
                        Message.incident_id == self._incident_id,
                        Message.provider_message_id == provider_message_id,
                    )
                )
                if raced is None:  # pragma: no cover - constraint without a winner
                    raise
                return AcceptResult(duplicate=True, message_id=raced.id)

            message.reply_to_message_id = await self._pending_clarification(
                session, message.thread_id, message.id
            )

            throttled = await self._throttle_reason(session, sender.id, now)
            if throttled is not None:
                limit, scope = throttled
                return await self._record_throttled(session, message, now, limit, scope)

            message.status = MessageStatus.DEFERRED if deferred else MessageStatus.QUEUED
            job = Job(
                id=uuid4(),
                incident_id=self._incident_id,
                type=JobType.FETCH_CONTENT if deferred else JobType.PROCESS_MESSAGE,
                payload={"message_id": str(message.id)},
                status=JobStatus.PENDING,
                dedupe_key=f"{'fetch' if deferred else 'process'}:{message.id}",
                available_at=now,
            )
            session.add(job)
            if not deferred:
                # Deadline acknowledgement: fires only if the message is
                # still unprocessed 15 s after acceptance; the handler
                # suppresses it when a final reply already exists.
                session.add(
                    Job(
                        id=uuid4(),
                        incident_id=self._incident_id,
                        type=JobType.PROCESS_MESSAGE,
                        payload={
                            "message_id": str(message.id),
                            "deadline_ack": True,
                        },
                        status=JobStatus.PENDING,
                        dedupe_key=f"ack:{message.id}",
                        available_at=now + DEADLINE_ACK_DELAY,
                    )
                )
            await record_event(
                session,
                incident_id=self._incident_id,
                event_type="message.deferred" if deferred else "message.accepted",
                aggregate_type="message",
                aggregate_id=message.id,
                payload={
                    "message_id": str(message.id),
                    "withheld": message.withheld,
                    "attachment_count": message.attachment_count,
                },
            )
            if message.attachment_count:
                await record_event(
                    session,
                    incident_id=self._incident_id,
                    event_type="message.attachments_ignored",
                    aggregate_type="message",
                    aggregate_id=message.id,
                    payload={"count": message.attachment_count},
                )
            await session.flush()
            return AcceptResult(duplicate=False, message_id=message.id, job_id=job.id)

    async def _resolve_sender(
        self, session: AsyncSession, address: str, now: datetime
    ) -> Sender:
        sender = await session.scalar(
            sa.select(Sender).where(
                Sender.incident_id == self._incident_id, Sender.address == address
            )
        )
        if sender is not None:
            return sender
        sender = Sender(id=uuid4(), incident_id=self._incident_id, address=address)
        try:
            async with session.begin_nested():
                session.add(sender)
                await session.flush()
        except IntegrityError:
            raced = await session.scalar(
                sa.select(Sender).where(
                    Sender.incident_id == self._incident_id, Sender.address == address
                )
            )
            if raced is None:  # pragma: no cover - constraint without a winner
                raise
            return raced
        await record_event(
            session,
            incident_id=self._incident_id,
            event_type="sender.created",
            actor_type="sender",
            actor_id=sender.id,
            aggregate_type="sender",
            aggregate_id=sender.id,
            payload={"sender_id": str(sender.id)},
        )
        return sender

    async def _pending_clarification(
        self,
        session: AsyncSession,
        thread_id: str | None,
        current_message_id: UUID,
    ) -> UUID | None:
        """Link a thread reply to the message whose clarification it answers.

        Linkage is by incident, thread, and previous message only. The
        clarification service re-checks the sender before merging, so a
        forwarded thread cannot mutate someone else's report.
        """
        if not thread_id:
            return None
        return await session.scalar(
            sa.select(Message.id)
            .where(
                Message.incident_id == self._incident_id,
                Message.thread_id == thread_id,
                Message.id != current_message_id,
                Message.status == MessageStatus.CLARIFICATION_PENDING,
            )
            .order_by(Message.received_at.desc())
            .limit(1)
        )

    async def _throttle_reason(
        self, session: AsyncSession, sender_id: UUID, now: datetime
    ) -> tuple[int, str] | None:
        since = now - _WINDOW
        sender_count = await session.scalar(
            sa.select(sa.func.count(Message.id)).where(
                Message.incident_id == self._incident_id,
                Message.sender_id == sender_id,
                Message.created_at >= since,
            )
        )
        if (sender_count or 0) > self._sender_limit:
            return self._sender_limit, "sender"
        incident_count = await session.scalar(
            sa.select(sa.func.count(Message.id)).where(
                Message.incident_id == self._incident_id,
                Message.created_at >= since,
            )
        )
        if (incident_count or 0) > self._incident_limit:
            return self._incident_limit, "incident"
        return None

    async def _record_throttled(
        self,
        session: AsyncSession,
        message: Message,
        now: datetime,
        limit: int,
        scope: str,
    ) -> AcceptResult:
        message.status = MessageStatus.THROTTLED
        sender = await session.get(Sender, message.sender_id)
        if sender is None:  # pragma: no cover - FK guarantees the row
            raise IngestionError("message sender disappeared")
        since = now - _WINDOW
        already_notified = await session.scalar(
            sa.select(OutboundEmail.id)
            .where(
                OutboundEmail.incident_id == self._incident_id,
                OutboundEmail.sender_id == sender.id,
                OutboundEmail.kind == "throttle_notice",
                OutboundEmail.created_at >= since,
            )
            .limit(1)
        )
        if already_notified is None:
            outbox_id = uuid4()
            notice = OutboundEmail(
                id=outbox_id,
                incident_id=self._incident_id,
                message_id=message.id,
                sender_id=sender.id,
                to_address=sender.address,
                subject="ResiliNet: message not processed",
                body_text=_throttle_body(limit, scope),
                kind="throttle_notice",
                idempotency_key=f"outbox:{outbox_id}",
                available_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(notice)
        await record_event(
            session,
            incident_id=self._incident_id,
            event_type="ingestion.throttled",
            aggregate_type="message",
            aggregate_id=message.id,
            payload={
                "message_id": str(message.id),
                "limit": limit,
                "scope": scope,
            },
        )
        await session.flush()
        return AcceptResult(duplicate=False, message_id=message.id, throttled=True)


__all__ = [
    "INCIDENT_LIMIT_PER_HOUR",
    "SENDER_LIMIT_PER_HOUR",
    "AcceptResult",
    "IngestionError",
    "IngestionService",
    "normalize_address",
    "normalize_inbound_text",
    "normalize_subject",
]

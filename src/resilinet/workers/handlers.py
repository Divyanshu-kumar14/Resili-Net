"""Durable handlers for inbound message jobs.

Each handler follows the same boundary: read in one short transaction, perform
model/HTTP work outside any transaction, then write the outcome in one
transaction that re-checks the message state under a row lock. A job retried
after a commit therefore becomes a no-op instead of a duplicate report.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db import utcnow
from ..models import Job, JobStatus, JobType, Message, MessageStatus, Sender
from ..ports import InboxProvider, MessageNotFoundError, ProviderUnavailableError, RawMessage
from ..schemas import ClarificationRequired, ParsedEmail, ReportKind
from ..services.clarification import (
    SPLIT_FIELD,
    ClarificationOutcome,
    ClarificationService,
)
from ..services.events import record_event
from ..services.extraction import ExtractionService, InjectionFinding
from ..services.ingestion import normalize_inbound_text
from ..services.notifications import NotificationService, OutboxService, reply_subject
from .runner import JobClaim, PermanentJobError

logger = logging.getLogger("resilinet.workers.handlers")

MATCHING_KINDS = frozenset({ReportKind.NEED, ReportKind.SUPPLY})
NOTICE_KINDS = frozenset(
    {ReportKind.QUERY, ReportKind.STATUS_UPDATE, ReportKind.CANCEL, ReportKind.OTHER}
)


class MessageJobHandler:
    """Dispatch the two Phase 1 job types against the durable message store."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        inbox: InboxProvider,
        extraction: ExtractionService,
        notifications: NotificationService,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._inbox = inbox
        self._extraction = extraction
        self._notifications = notifications
        self._clock = clock or utcnow

    async def __call__(self, claim: JobClaim) -> None:
        if claim.type is JobType.PROCESS_MESSAGE:
            await self._process(claim)
        elif claim.type is JobType.FETCH_CONTENT:
            await self._fetch(claim)
        else:  # pragma: no cover - the runner only registers these two
            raise PermanentJobError(f"unsupported job type {claim.type.value}")

    # -- process -----------------------------------------------------------

    async def _process(self, claim: JobClaim) -> None:
        message_id = self._message_id(claim)
        snapshot = await self._load_message(message_id)
        if snapshot is None:
            raise PermanentJobError("message no longer exists")
        message, sender = snapshot
        if message.status is not MessageStatus.QUEUED:
            # Already handled, throttled, deferred, or under review.
            return

        findings: list[InjectionFinding] = []
        try:
            parsed = await self._extraction.parse(
                _raw_message(message, sender), audit=findings.append
            )
        finally:
            try:
                await self._record_injection_findings(claim, findings)
            except Exception:
                logger.exception(
                    "injection_audit_write_failed", extra={"job_id": str(claim.id)}
                )

        now = self._clock()
        async with self._session_factory() as session, session.begin():
            current = await session.get(Message, message_id, with_for_update=True)
            if current is None:
                raise PermanentJobError("message no longer exists")
            if current.status is not MessageStatus.QUEUED:
                return
            clarification = ClarificationService(
                session, self._notifications, clock=lambda: now
            )
            original = (
                await session.get(
                    Message, current.reply_to_message_id, with_for_update=True
                )
                if current.reply_to_message_id is not None
                else None
            )
            if original is not None:
                await self._handle_correction(clarification, current, original, parsed)
            else:
                await self._handle_new(session, clarification, current, parsed)

    async def _handle_new(
        self,
        session: AsyncSession,
        clarification: ClarificationService,
        message: Message,
        parsed: ParsedEmail | ClarificationRequired,
    ) -> None:
        if isinstance(parsed, ClarificationRequired):
            if parsed.parsed is None:
                await clarification.escalate(
                    message,
                    None,
                    reason="extraction could not retain a partial report",
                    event_type="clarification.state_missing",
                    notice=self._notifications.human_review_text(
                        "the report could not be parsed"
                    ),
                )
                return
            await clarification.ask(
                message, clarification.outcome_for_required(parsed), parsed.parsed
            )
            return

        decision = clarification.evaluate(parsed, message)
        if isinstance(decision, ClarificationOutcome):
            await clarification.ask(message, decision, parsed)
            return

        if parsed.context.kind in MATCHING_KINDS:
            # Persist the evidence. The threaded recommendation is Task 12's
            # single logical notification for this inbound message, so Phase 1
            # deliberately queues no competing acknowledgement.
            await clarification.process_ready(message, parsed)
            return

        if parsed.context.kind not in NOTICE_KINDS:  # pragma: no cover - enum is closed
            raise PermanentJobError(f"unhandled report kind {parsed.context.kind.value}")
        message.status = MessageStatus.PROCESSED
        message.parsed_json = parsed.model_dump(mode="json")
        message.processed_at = self._clock()
        body = (
            self._notifications.query_text()
            if parsed.context.kind in (ReportKind.QUERY, ReportKind.OTHER)
            else self._notifications.action_required_text()
        )
        await self._enqueue_notice(
            session,
            message,
            kind=f"notice:{parsed.context.kind.value.lower()}",
            body=body,
        )
        await record_event(
            session,
            incident_id=message.incident_id,
            event_type="message.noted",
            aggregate_type="message",
            aggregate_id=message.id,
            payload={"message_id": str(message.id), "kind": parsed.context.kind.value},
        )

    async def _handle_correction(
        self,
        clarification: ClarificationService,
        correction: Message,
        original: Message,
        parsed: ParsedEmail | ClarificationRequired,
    ) -> None:
        if isinstance(parsed, ClarificationRequired):
            if parsed.parsed is None:
                await clarification.escalate(
                    original,
                    correction,
                    reason="correction could not be parsed",
                    event_type="clarification.escalated",
                    notice=self._notifications.human_review_text(),
                )
                return
            requested = set(original.clarification_fields)
            blocking = set(parsed.fields) & (requested | {SPLIT_FIELD})
            if blocking:
                await clarification.escalate(
                    original,
                    correction,
                    reason="correction is still ambiguous: "
                    + ", ".join(sorted(blocking)),
                    event_type="clarification.escalated",
                    notice=self._notifications.human_review_text(),
                )
                return
            correction_parsed = parsed.parsed
        else:
            correction_parsed = parsed
        await clarification.handle_correction(original, correction, correction_parsed)

    async def _enqueue_notice(
        self,
        session: AsyncSession,
        message: Message,
        *,
        kind: str,
        body: str,
    ) -> None:
        sender = await session.get(Sender, message.sender_id)
        if sender is None:  # pragma: no cover - FK guarantees the row
            raise PermanentJobError("message sender disappeared")
        await OutboxService.enqueue_reply(
            session,
            message=message,
            sender=sender,
            subject=reply_subject(message.subject),
            body=body,
            kind=kind,
            available_at=self._clock(),
        )

    # -- fetch -------------------------------------------------------------

    async def _fetch(self, claim: JobClaim) -> None:
        message_id = self._message_id(claim)
        snapshot = await self._load_message(message_id)
        if snapshot is None:
            raise PermanentJobError("message no longer exists")
        message, _ = snapshot
        if message.status is not MessageStatus.DEFERRED:
            return
        try:
            fetched = await self._inbox.fetch_message(message.provider_message_id)
        except MessageNotFoundError as exc:
            raise PermanentJobError(f"provider no longer has the message: {exc}") from exc

        body = normalize_inbound_text(fetched.text)
        if fetched.withheld:
            # Partial content is not authoritative: keep the message deferred
            # and retry until the provider releases the body.
            raise ProviderUnavailableError(
                "provider still withholds the message content", retryable=True
            )

        now = self._clock()
        async with self._session_factory() as session, session.begin():
            current = await session.get(Message, message_id, with_for_update=True)
            if current is None:
                raise PermanentJobError("message no longer exists")
            if current.status is not MessageStatus.DEFERRED:
                return
            current.raw_text = fetched.text
            current.body_text = body
            current.withheld = fetched.withheld
            current.attachment_count = fetched.attachment_count
            current.status = MessageStatus.QUEUED
            session.add(
                Job(
                    incident_id=current.incident_id,
                    type=JobType.PROCESS_MESSAGE,
                    payload={"message_id": str(current.id)},
                    status=JobStatus.PENDING,
                    dedupe_key=f"process:{current.id}",
                    available_at=now,
                )
            )
            await record_event(
                session,
                incident_id=current.incident_id,
                event_type="message.content_fetched",
                aggregate_type="message",
                aggregate_id=current.id,
                payload={"message_id": str(current.id)},
            )

    # -- helpers -----------------------------------------------------------

    async def _load_message(self, message_id: UUID) -> tuple[Message, Sender] | None:
        async with self._session_factory() as session:
            message = await session.get(Message, message_id)
            if message is None:
                return None
            sender = await session.get(Sender, message.sender_id)
            if sender is None:  # pragma: no cover - FK guarantees the row
                raise PermanentJobError("message sender disappeared")
            return message, sender

    async def _record_injection_findings(
        self, claim: JobClaim, findings: list[InjectionFinding]
    ) -> None:
        if not findings:
            return
        async with self._session_factory() as session, session.begin():
            for finding in findings:
                await record_event(
                    session,
                    incident_id=finding.incident_id,
                    event_type="security.extraction_injection",
                    trace_id=finding.trace_id,
                    payload={
                        "patterns": list(finding.patterns),
                        "message_ref": finding.message_ref,
                    },
                )
        logger.warning(
            "extraction_injection_detected",
            extra={
                "job_id": str(claim.id),
                "patterns": ",".join(
                    sorted({pattern for finding in findings for pattern in finding.patterns})
                ),
            },
        )

    @staticmethod
    def _message_id(claim: JobClaim) -> UUID:
        value = claim.payload.get("message_id")
        if not isinstance(value, str):
            raise PermanentJobError("job payload has no message_id")
        try:
            return UUID(value)
        except ValueError as exc:
            raise PermanentJobError("job payload has an invalid message_id") from exc


def _raw_message(message: Message, sender: Sender) -> RawMessage:
    return RawMessage(
        provider_message_id=message.provider_message_id,
        mailbox_id=message.mailbox_id,
        sender=sender.address,
        subject=message.subject,
        text=message.body_text,
        received_at=message.received_at,
        thread_id=message.thread_id,
        attachment_count=message.attachment_count,
        withheld=message.withheld,
    )


__all__ = ["NOTICE_KINDS", "MessageJobHandler"]

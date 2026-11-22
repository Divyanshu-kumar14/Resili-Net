from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.adapters.fakes import FakeGeocoder, FakeInboxProvider, provider_unavailable
from resilinet.db import transaction
from resilinet.domain.references import (
    ReferenceSequence,
    new_reference,
    parse_reference,
)
from resilinet.models import (
    EventLog,
    Incident,
    Job,
    JobStatus,
    JobType,
    Message,
    MessageStatus,
    OutboundEmail,
    OutboundStatus,
    Sender,
)
from resilinet.ports import InboundEvent, RawMessage
from resilinet.services.extraction import ExtractionService
from resilinet.services.ingestion import (
    IngestionService,
    normalize_address,
    normalize_inbound_text,
)
from resilinet.services.notifications import OutboxService
from resilinet.workers.handlers import MessageJobHandler
from resilinet.workers.runner import DurableWorker, JobClaim

from .conftest import NOW, Clock, alembic_revision, table_names

CORE_TABLES = {
    "incidents",
    "senders",
    "participant_roles",
    "messages",
    "reports",
    "entities",
    "needs",
    "supplies",
    "allocations",
    "pending_actions",
    "review_cases",
    "event_log",
    "outbound_emails",
    "jobs",
    "verification_requests",
    "action_grants",
    "sessions",
    "geocode_cache",
}


def make_event(
    *,
    provider_message_id: str = "msg-1",
    sender: str = "Volunteer@Example.org",
    subject: str = "Shelter 4 water",
    text: str = "Shelter 4 has 40 people and zero potable water. Near Gandhi Park.",
    withheld: bool = False,
    thread_id: str | None = "thread-1",
    attachment_count: int = 0,
    received_at: datetime = NOW,
    event_type: str = "message.received",
    event_id: str = "evt-1",
    mailbox_id: str = "mbx-1",
) -> InboundEvent:
    raw = (
        None
        if text is None
        else RawMessage(
            provider_message_id=provider_message_id,
            mailbox_id=mailbox_id,
            sender=sender,
            subject=subject,
            text=text,
            received_at=received_at,
            thread_id=thread_id,
            attachment_count=attachment_count,
        )
    )
    return InboundEvent(
        event_id=event_id,
        event_type=event_type,
        mailbox_id=mailbox_id,
        provider_message_id=provider_message_id,
        sender=sender,
        subject=subject,
        received_at=received_at,
        withheld=withheld,
        mailbox_address="incident@example.org",
        thread_id=thread_id,
        raw=raw,
    )


def test_migration_creates_the_core_schema(database_path: Path) -> None:
    assert table_names(database_path) >= CORE_TABLES
    assert alembic_revision(database_path) == "0004"


async def test_duplicate_event_creates_one_message_and_one_job(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    ingestion = IngestionService(incident_id, session_factory)
    event = make_event()

    first = await ingestion.accept(event)
    second = await ingestion.accept(event)

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.message_id == first.message_id
    async with session_factory() as session:
        messages = await session.scalar(
            sa.select(sa.func.count(Message.id)).where(Message.incident_id == incident_id)
        )
        jobs = await session.scalar(sa.select(sa.func.count(Job.id)))
        process_jobs = await session.scalar(
            sa.select(sa.func.count(Job.id)).where(Job.dedupe_key.like("process:%"))
        )
        ack_jobs = await session.scalar(
            sa.select(sa.func.count(Job.id)).where(Job.dedupe_key.like("ack:%"))
        )
        accepted = await session.scalar(
            sa.select(sa.func.count(EventLog.id)).where(
                EventLog.event_type == "message.accepted"
            )
        )
    assert messages == 1
    # One processing job (the dedupe invariant) plus its deadline-ack job.
    assert process_jobs == 1
    assert ack_jobs == 1
    assert jobs == 2
    assert accepted == 1


async def test_same_provider_id_is_isolated_per_incident(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    async with transaction(session_factory) as session:
        other = Incident(key="other", name="Other Incident", simulation=True)
        session.add(other)
        await session.flush()
        other_id = other.id

    first = await IngestionService(incident_id, session_factory).accept(make_event())
    second = await IngestionService(other_id, session_factory).accept(make_event())

    assert first.duplicate is False
    assert second.duplicate is False
    async with session_factory() as session:
        count = await session.scalar(sa.select(sa.func.count(Message.id)))
    assert count == 2


async def test_withheld_envelope_is_deferred_then_fetched(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    inbox = FakeInboxProvider(webhook_secret="secret", now=clock)
    mailbox = await inbox.provision_inbox("demo-incident")
    inbox.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-withheld",
        sender="volunteer@example.org",
        subject="Shelter 4 water",
        text="Shelter 4 has 40 people and zero potable water. Near Gandhi Park.",
        thread_id="thread-1",
        received_at=NOW,
    )
    ingestion = IngestionService(incident_id, session_factory, clock=clock)
    accepted = await ingestion.accept(
        make_event(
            provider_message_id="msg-withheld",
            subject="[not screened yet]",
            text=None,
            withheld=True,
            thread_id=None,
            mailbox_id=mailbox.id,
        )
    )

    async with session_factory() as session:
        message = await session.get(Message, accepted.message_id)
        jobs = list(await session.scalars(sa.select(Job)))
    assert message is not None and message.status is MessageStatus.DEFERRED
    assert [job.type for job in jobs] == [JobType.FETCH_CONTENT]

    handler = MessageJobHandler(
        session_factory=session_factory,
        inbox=inbox,
        extraction=ExtractionService(incident_id, _unused_intelligence(), FakeGeocoder({})),
        notifications=_notifications(),
        intelligence=_unused_intelligence(),
        public_base_url="http://testserver",
        clock=clock,
    )
    worker = DurableWorker(
        session_factory, {JobType.FETCH_CONTENT: handler}, clock=clock, jitter=lambda: 0.0
    )
    assert await worker.run_once() == 1

    async with session_factory() as session:
        message = await session.get(Message, accepted.message_id)
        process_job = await session.scalar(
            sa.select(Job).where(Job.dedupe_key == f"process:{accepted.message_id}")
        )
    assert message is not None
    assert message.status is MessageStatus.QUEUED
    assert message.withheld is False
    assert message.subject == "Shelter 4 water"
    assert message.thread_id == "thread-1"
    assert "zero potable water" in message.body_text
    assert process_job is not None


def test_quote_and_signature_are_stripped() -> None:
    text = (
        "Shelter 4 has 40 people and zero water.\n"
        "On Sat, Nov 21, 2026 at 9:00 AM Coordinator <c@example.org> wrote:\n"
        "> please confirm the headcount\n"
        "-- \n"
        "Volunteer Name\n"
        "+91 00000 00000\n"
    )

    assert normalize_inbound_text(text) == "Shelter 4 has 40 people and zero water."


def test_unicode_normalization_and_empty_after_strip() -> None:
    assert normalize_inbound_text("  \uff33helter\u00a04\u3000water  ") == "Shelter 4 water"
    # A message that is only a quote keeps its content rather than being lost.
    only_quote = "> quoted original\n> second line"
    assert normalize_inbound_text(only_quote) == "> quoted original\n> second line"


def test_address_identity_is_case_insensitive() -> None:
    assert normalize_address("  Volunteer@Example.ORG ") == "volunteer@example.org"


async def test_expired_lease_is_reclaimed(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    async with transaction(session_factory) as session:
        session.add(
            Job(
                incident_id=incident_id,
                type=JobType.PROCESS_MESSAGE,
                payload={"message_id": str(uuid4())},
                status=JobStatus.LEASED,
                lease_owner="dead-worker",
                lease_expires_at=NOW - timedelta(seconds=5),
                attempts=1,
                available_at=NOW - timedelta(seconds=60),
            )
        )

    calls: list[JobClaim] = []

    async def handler(claim: JobClaim) -> None:
        calls.append(claim)

    worker = DurableWorker(
        session_factory,
        {JobType.PROCESS_MESSAGE: handler},
        clock=clock,
        jitter=lambda: 0.0,
    )
    assert await worker.run_once() == 1
    assert len(calls) == 1
    assert calls[0].attempts == 2

    async with session_factory() as session:
        job = await session.scalar(sa.select(Job))
    assert job is not None and job.status is JobStatus.SUCCEEDED


async def test_a_live_lease_is_not_stolen(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    async with transaction(session_factory) as session:
        session.add(
            Job(
                incident_id=incident_id,
                type=JobType.PROCESS_MESSAGE,
                payload={"message_id": str(uuid4())},
                status=JobStatus.LEASED,
                lease_owner="alive-worker",
                lease_expires_at=NOW + timedelta(seconds=60),
                attempts=1,
                available_at=NOW,
            )
        )

    worker = DurableWorker(session_factory, {}, clock=clock, jitter=lambda: 0.0)
    assert await worker.run_once() == 0


async def test_transient_failure_retries_then_dead_letters(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    async with transaction(session_factory) as session:
        session.add(
            Job(
                incident_id=incident_id,
                type=JobType.PROCESS_MESSAGE,
                payload={"message_id": str(uuid4())},
                status=JobStatus.PENDING,
                max_attempts=2,
                available_at=NOW,
            )
        )

    async def failing(_claim: JobClaim) -> None:
        raise provider_unavailable("temporary outage")

    worker = DurableWorker(
        session_factory,
        {JobType.PROCESS_MESSAGE: failing},
        clock=clock,
        jitter=lambda: 0.0,
        retry_base_seconds=10.0,
    )
    assert await worker.run_once() == 1
    async with session_factory() as session:
        job = await session.scalar(sa.select(Job))
    assert job is not None
    assert job.status is JobStatus.RETRY_SCHEDULED
    assert job.available_at == NOW + timedelta(seconds=10)

    clock.advance(seconds=30)
    assert await worker.run_once() == 1
    async with session_factory() as session:
        job = await session.scalar(sa.select(Job))
        dead_letter = await session.scalar(
            sa.select(EventLog).where(EventLog.event_type == "job.dead_letter")
        )
    assert job is not None and job.status is JobStatus.DEAD_LETTER
    assert dead_letter is not None


async def test_rate_limit_queues_a_sender_visible_notice(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    inbox = FakeInboxProvider(webhook_secret="secret", now=clock)
    mailbox = await inbox.provision_inbox("demo-incident")
    ingestion = IngestionService(
        incident_id, session_factory, clock=clock, sender_limit=2
    )
    results = []
    for index in range(4):
        provider_id = f"msg-rate-{index}"
        inbox.deliver_inbound(
            mailbox_id=mailbox.id,
            provider_message_id=provider_id,
            sender="volunteer@example.org",
            subject=f"update {index}",
            text="Shelter 4 needs water near Gandhi Park.",
            received_at=NOW,
        )
        results.append(
            await ingestion.accept(
                make_event(
                    provider_message_id=provider_id,
                    event_id=f"evt-{index}",
                    thread_id=f"thread-{index}",
                    mailbox_id=mailbox.id,
                )
            )
        )

    assert [result.throttled for result in results] == [False, False, True, True]
    async with session_factory() as session:
        throttled = list(
            await session.scalars(
                sa.select(Message).where(Message.status == MessageStatus.THROTTLED)
            )
        )
        jobs = await session.scalar(sa.select(sa.func.count(Job.id)))
        notices = list(await session.scalars(sa.select(OutboundEmail)))
    assert len(throttled) == 2
    # Two accepted messages x (process + deadline-ack); throttled get neither.
    assert jobs == 4
    # At most one notice per sender per window prevents backscatter abuse.
    assert len(notices) == 1
    assert notices[0].kind == "throttle_notice"

    outbox = OutboxService(session_factory, inbox, clock=clock)
    assert await outbox.deliver_due() is True
    sent = inbox.sent_messages
    assert len(sent) == 1
    assert "intake limit" in sent[0].draft.text
    assert sent[0].draft.to == "volunteer@example.org"
    assert sent[0].idempotency_key.startswith("outbox:")

    # A duplicate provider event must not send a second notice.
    assert await outbox.deliver_due() is False


async def test_attachments_are_ignored_with_an_audit_event(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    ingestion = IngestionService(incident_id, session_factory)
    accepted = await ingestion.accept(make_event(attachment_count=3))

    async with session_factory() as session:
        event = await session.scalar(
            sa.select(EventLog).where(EventLog.event_type == "message.attachments_ignored")
        )
        message = await session.get(Message, accepted.message_id)
    assert event is not None and event.payload["count"] == 3
    assert message is not None and message.attachment_count == 3


async def test_reference_codes_are_six_digit_unique_and_parseable(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    async with transaction(session_factory) as session:
        first = await new_reference(session, "RN", start_offset=424242)
        second = await new_reference(session, "RN")
        allocation = await new_reference(session, "AL")

    assert first == "RN-424242"
    assert second == "RN-424243"
    assert allocation.startswith("AL-")
    assert parse_reference(first) == ("RN", 424242)
    assert len(first) == 9
    # The Phase 0 in-memory sequence cannot emit an enumerable RN-000001 either.
    assert parse_reference(ReferenceSequence().next("RN"))[1] >= 100_000


async def test_clarification_pending_thread_is_linked_on_intake(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    ingestion = IngestionService(incident_id, session_factory)
    original = await ingestion.accept(make_event(provider_message_id="msg-orig"))
    async with transaction(session_factory) as session:
        row = await session.get(Message, original.message_id)
        assert row is not None
        row.status = MessageStatus.CLARIFICATION_PENDING

    reply = await ingestion.accept(
        make_event(provider_message_id="msg-reply", event_id="evt-reply")
    )

    async with session_factory() as session:
        row = await session.get(Message, reply.message_id)
    assert row is not None
    assert row.reply_to_message_id == original.message_id


def _notifications() -> Any:
    from resilinet.services.notifications import NotificationService

    return NotificationService()


def _unused_intelligence() -> Any:
    from resilinet.adapters.fakes import FakeIntelligenceProvider
    from resilinet.schemas import (
        FieldConfidence,
        ParsedEmail,
        ParsedReport,
        ReportContext,
        ReportKind,
        Resource,
    )

    fixture = ParsedEmail(
        context=ReportContext(
            kind=ReportKind.NEED,
            entity_name="Shelter 4",
            headcount=40,
            confidence=FieldConfidence(location=0.9, items=0.9, headcount=0.9),
        ),
        reports=[ParsedReport(resource=Resource.POTABLE_WATER)],
    )
    return FakeIntelligenceProvider({"unused": fixture})


async def test_incident_limit_notice_names_the_incident(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    inbox = FakeInboxProvider(webhook_secret="secret", now=clock)
    mailbox = await inbox.provision_inbox("demo-incident")
    ingestion = IngestionService(
        incident_id,
        session_factory,
        clock=clock,
        sender_limit=100,
        incident_limit=1,
    )
    first = await ingestion.accept(
        make_event(
            provider_message_id="msg-first",
            event_id="evt-first",
            sender="first@example.org",
            mailbox_id=mailbox.id,
        )
    )
    second = await ingestion.accept(
        make_event(
            provider_message_id="msg-second",
            event_id="evt-second",
            sender="second@example.org",
            mailbox_id=mailbox.id,
        )
    )

    assert first.throttled is False
    assert second.throttled is True
    async with session_factory() as session:
        notice = await session.scalar(sa.select(OutboundEmail))
        event = await session.scalar(
            sa.select(EventLog).where(EventLog.event_type == "ingestion.throttled")
        )
    assert notice is not None
    assert "this incident" in notice.body_text
    assert "this sender" not in notice.body_text
    assert event is not None and event.payload["scope"] == "incident"


async def test_withheld_event_with_partial_body_still_defers(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    inbox = FakeInboxProvider(webhook_secret="secret", now=clock)
    mailbox = await inbox.provision_inbox("demo-incident")
    inbox.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-partial",
        sender="volunteer@example.org",
        subject="Shelter 4 water",
        text="the full body the provider is holding",
        received_at=NOW,
        withheld=True,
    )
    ingestion = IngestionService(incident_id, session_factory, clock=clock)

    accepted = await ingestion.accept(
        make_event(
            provider_message_id="msg-partial",
            text="a partial snippet",
            withheld=True,
            mailbox_id=mailbox.id,
        )
    )

    async with session_factory() as session:
        message = await session.get(Message, accepted.message_id)
        jobs = list(await session.scalars(sa.select(Job)))
    assert message is not None and message.status is MessageStatus.DEFERRED
    assert [job.type for job in jobs] == [JobType.FETCH_CONTENT]


async def test_a_stale_worker_result_is_discarded(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    async with transaction(session_factory) as session:
        job = Job(
            incident_id=incident_id,
            type=JobType.PROCESS_MESSAGE,
            payload={"message_id": str(uuid4())},
            status=JobStatus.LEASED,
            lease_owner="live-worker",
            lease_expires_at=NOW + timedelta(seconds=60),
            attempts=2,
            available_at=NOW,
        )
        session.add(job)
        await session.flush()
        job_id = job.id

    worker = DurableWorker(session_factory, {}, worker_id="stale-worker", clock=clock)
    stale_claim = JobClaim(
        id=job_id,
        incident_id=incident_id,
        type=JobType.PROCESS_MESSAGE,
        payload={},
        attempts=1,
        max_attempts=3,
    )
    await worker._succeed(stale_claim)  # deliberate fence test of the terminal write

    async with session_factory() as session:
        reloaded = await session.get(Job, job_id)
    assert reloaded is not None
    assert reloaded.status is JobStatus.LEASED
    assert reloaded.lease_owner == "live-worker"
    assert reloaded.attempts == 2


async def test_a_stale_sending_outbox_row_is_reclaimed(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    inbox = FakeInboxProvider(webhook_secret="secret", now=clock)
    mailbox = await inbox.provision_inbox("demo-incident")
    inbox.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-stale-out",
        sender="volunteer@example.org",
        subject="Re: Shelter 4 water",
        text="original",
        received_at=NOW,
    )
    outbox_id = uuid4()
    async with transaction(session_factory) as session:
        sender = Sender(incident_id=incident_id, address="volunteer@example.org")
        session.add(sender)
        await session.flush()
        message = Message(
            incident_id=incident_id,
            sender_id=sender.id,
            provider_message_id="msg-stale-out",
            mailbox_id=mailbox.id,
            subject="Shelter 4 water",
            body_text="original",
            raw_text="original",
            received_at=NOW,
        )
        session.add(message)
        await session.flush()
        session.add(
            OutboundEmail(
                id=outbox_id,
                incident_id=incident_id,
                message_id=message.id,
                sender_id=sender.id,
                to_address=sender.address,
                subject="Re: Shelter 4 water",
                body_text="a queued reply",
                kind="clarification",
                idempotency_key=f"outbox:{outbox_id}",
                status=OutboundStatus.SENDING,
                attempt_count=1,
                available_at=NOW - timedelta(minutes=10),
                created_at=NOW - timedelta(minutes=10),
                updated_at=NOW - timedelta(minutes=10),
            )
        )

    outbox = OutboxService(
        session_factory, inbox, clock=clock, stale_seconds=60.0
    )

    assert await outbox.deliver_due() is True
    assert len(inbox.sent_messages) == 1
    async with session_factory() as session:
        row = await session.get(OutboundEmail, outbox_id)
    assert row is not None and row.status is OutboundStatus.SENT

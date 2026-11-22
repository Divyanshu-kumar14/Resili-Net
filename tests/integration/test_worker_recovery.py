"""Task 21: bounded retries, dead letters, deadline acks, readiness.

Every failure must end in an auditable sender/operator outcome: a retry
with backoff, or a dead letter plus an operator review case and a sender
notice. A message that is still unprocessed 15 s after acceptance gets
exactly one acknowledgement; a finished message never gets one.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.adapters.fakes import (
    FakeGeocoder,
    FakeInboxProvider,
    FakeIntelligenceProvider,
)
from resilinet.config import Settings
from resilinet.db import transaction
from resilinet.main import Container, create_app
from resilinet.models import (
    EventLog,
    Job,
    JobStatus,
    JobType,
    Need,
    OutboundEmail,
    ReviewCase,
    ReviewCaseKind,
)
from resilinet.ports import ExtractionError, ProviderUnavailableError
from resilinet.schemas import (
    EntityType,
    FieldConfidence,
    ParsedEmail,
    ParsedReport,
    ReportContext,
    ReportKind,
    Resource,
    Urgency,
)
from resilinet.services.extraction import ExtractionService
from resilinet.services.ingestion import IngestionService
from resilinet.services.notifications import NotificationService, OutboxService
from resilinet.workers.handlers import MessageJobHandler
from resilinet.workers.runner import DurableWorker, JobClaim

from .conftest import NOW, Clock
from .test_durable_pipeline import PLACES, deliver

CONFIDENCE = FieldConfidence(location=0.95, items=0.95, headcount=0.95)


def need_parsed() -> ParsedEmail:
    return ParsedEmail(
        context=ReportContext(
            kind=ReportKind.NEED,
            entity_name="Shelter 4",
            entity_type=EntityType.SHELTER,
            location_text="near Gandhi Park",
            urgency=Urgency.HIGH,
            confidence=CONFIDENCE,
        ),
        reports=[
            ParsedReport(
                resource=Resource.POTABLE_WATER, quantity=Decimal("100"), unit="l"
            )
        ],
    )


@dataclass
class Harness:
    inbox: FakeInboxProvider
    mailbox_id: str
    worker: DurableWorker
    outbox: OutboxService
    intelligence: FakeIntelligenceProvider
    ingestion: IngestionService


async def build_recovery_harness(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> Harness:
    inbox = FakeInboxProvider(webhook_secret="secret", now=clock)
    mailbox = await inbox.provision_inbox("demo-incident")
    intelligence = FakeIntelligenceProvider({"recovery-need": need_parsed()})
    handler = MessageJobHandler(
        session_factory=session_factory,
        inbox=inbox,
        extraction=ExtractionService(incident_id, intelligence, FakeGeocoder(PLACES)),
        notifications=NotificationService(),
        intelligence=intelligence,
        public_base_url="http://testserver",
        clock=clock,
    )
    worker = DurableWorker(
        session_factory,
        {JobType.PROCESS_MESSAGE: handler, JobType.FETCH_CONTENT: handler},
        clock=clock,
        jitter=lambda: 0.0,
        worker_id="recovery-worker",
    )
    harness = Harness(
        inbox=inbox,
        mailbox_id=mailbox.id,
        worker=worker,
        outbox=OutboxService(session_factory, inbox, clock=clock),
        intelligence=intelligence,
        ingestion=IngestionService(incident_id, session_factory, clock=clock),
    )
    return harness


async def accept(harness: Harness, provider_id: str, body: str) -> None:
    # Reuse the durable-pipeline deliver() accept path via its harness shape.
    from .test_durable_pipeline import Harness as PipelineHarness

    shim = PipelineHarness(
        inbox=harness.inbox,
        mailbox_id=harness.mailbox_id,
        ingestion=harness.ingestion,
        worker=harness.worker,
        outbox=harness.outbox,
    )
    await deliver(
        shim,
        provider_message_id=provider_id,
        subject="recovery-need",
        text=body,
        sender="volunteer@example.org",
    )


async def job_for(session_factory, incident_id: UUID, job_type: JobType) -> Job | None:
    async with session_factory() as session:
        return await session.scalar(
            sa.select(Job)
            .where(
                Job.incident_id == incident_id,
                Job.type == job_type,
                Job.dedupe_key.like("process:%"),
            )
            .order_by(Job.created_at.desc())
        )


async def ack_job_for(session_factory, incident_id: UUID) -> Job | None:
    async with session_factory() as session:
        return await session.scalar(
            sa.select(Job).where(
                Job.incident_id == incident_id,
                Job.dedupe_key.like("ack:%"),
            )
        )


async def drain(harness: Harness) -> None:
    for _ in range(25):
        if await harness.worker.run_once() == 0:
            break
    while await harness.outbox.deliver_due():
        pass


async def outbound_kinds(session_factory, incident_id: UUID) -> list[str]:
    async with session_factory() as session:
        return list(
            await session.scalars(
                sa.select(OutboundEmail.kind).where(
                    OutboundEmail.incident_id == incident_id
                )
            )
        )


async def test_transient_error_retries_with_backoff_then_succeeds(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    harness = await build_recovery_harness(session_factory, incident_id, clock)
    harness.intelligence.fail_next_extract = ProviderUnavailableError(
        "provider down", retryable=True
    )
    await accept(harness, "msg-retry", "recovery-need: water please")

    assert await harness.worker.run_once() == 1
    job = await job_for(session_factory, incident_id, JobType.PROCESS_MESSAGE)
    assert job is not None
    assert job.status is JobStatus.RETRY_SCHEDULED
    assert job.attempts == 1
    assert job.available_at > NOW
    assert job.last_error and "ProviderUnavailableError" in job.last_error

    clock.advance(seconds=30)
    await drain(harness)
    job = await job_for(session_factory, incident_id, JobType.PROCESS_MESSAGE)
    assert job is not None and job.status is JobStatus.SUCCEEDED
    async with session_factory() as session:
        needs = int(
            await session.scalar(
                sa.select(sa.func.count(Need.id)).where(Need.incident_id == incident_id)
            )
            or 0
        )
    assert needs == 1


async def test_schema_error_dead_letters_without_blind_retry(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    harness = await build_recovery_harness(session_factory, incident_id, clock)
    harness.intelligence.fail_next_extract = ExtractionError("schema invalid")
    await accept(harness, "msg-schema", "recovery-need: water please")

    assert await harness.worker.run_once() == 1
    job = await job_for(session_factory, incident_id, JobType.PROCESS_MESSAGE)
    assert job is not None
    assert job.status is JobStatus.DEAD_LETTER
    assert job.attempts == 1

    # Terminal failure is auditable: operator review + sender notice.
    async with session_factory() as session:
        reviews = int(
            await session.scalar(
                sa.select(sa.func.count(ReviewCase.id)).where(
                    ReviewCase.incident_id == incident_id,
                    ReviewCase.kind == ReviewCaseKind.DEAD_LETTER,
                )
            )
            or 0
        )
    assert reviews == 1
    kinds = await outbound_kinds(session_factory, incident_id)
    assert any(kind.startswith("processing_failed") for kind in kinds)


async def test_third_attempt_dead_letter_reviews_and_notifies_once(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    harness = await build_recovery_harness(session_factory, incident_id, clock)
    await accept(harness, "msg-dead", "recovery-need: water please")

    for _ in range(8):
        harness.intelligence.fail_next_extract = ProviderUnavailableError(
            "still down", retryable=True
        )
        if await harness.worker.run_once() == 0:
            break
        clock.advance(seconds=30)
        job = await job_for(session_factory, incident_id, JobType.PROCESS_MESSAGE)
        if job is not None and job.status is JobStatus.DEAD_LETTER:
            break

    job = await job_for(session_factory, incident_id, JobType.PROCESS_MESSAGE)
    assert job is not None
    assert job.status is JobStatus.DEAD_LETTER
    assert job.attempts == 3

    await drain(harness)
    kinds = await outbound_kinds(session_factory, incident_id)
    assert sum(kind.startswith("processing_failed") for kind in kinds) == 1
    async with session_factory() as session:
        reviews = int(
            await session.scalar(
                sa.select(sa.func.count(ReviewCase.id)).where(
                    ReviewCase.incident_id == incident_id,
                    ReviewCase.kind == ReviewCaseKind.DEAD_LETTER,
                )
            )
            or 0
        )
    assert reviews == 1


async def test_deadline_ack_sent_exactly_once_for_slow_processing(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    harness = await build_recovery_harness(session_factory, incident_id, clock)
    await accept(harness, "msg-slow", "recovery-need: water please")

    ack_job = await ack_job_for(session_factory, incident_id)
    assert ack_job is not None, "acceptance must schedule a deadline ack"
    assert ack_job.available_at == NOW + timedelta(seconds=15)

    # The queue is busy: the process job is not picked up before the deadline.
    async with transaction(session_factory) as session:
        process_job = await session.scalar(
            sa.select(Job).where(
                Job.incident_id == incident_id,
                Job.type == JobType.PROCESS_MESSAGE,
                Job.dedupe_key.like("process:%"),
            )
        )
        assert process_job is not None
        process_job.available_at = NOW + timedelta(hours=1)

    clock.advance(seconds=16)
    await drain(harness)
    kinds = await outbound_kinds(session_factory, incident_id)
    assert sum(kind.startswith("deadline_ack") for kind in kinds) == 1

    # Nothing re-sends it.
    await drain(harness)
    kinds = await outbound_kinds(session_factory, incident_id)
    assert sum(kind.startswith("deadline_ack") for kind in kinds) == 1
    ack_job = await ack_job_for(session_factory, incident_id)
    assert ack_job is not None and ack_job.status is JobStatus.SUCCEEDED


async def test_final_reply_suppresses_deadline_ack(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    harness = await build_recovery_harness(session_factory, incident_id, clock)
    await accept(harness, "msg-fast", "recovery-need: water please")
    await drain(harness)  # processed; final reply queued and sent

    clock.advance(seconds=30)
    await drain(harness)
    kinds = await outbound_kinds(session_factory, incident_id)
    assert kinds, "the processed message should have produced a reply"
    assert not any(kind.startswith("deadline_ack") for kind in kinds)


async def test_lease_heartbeat_extends_lease_during_long_handler(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
) -> None:
    started = asyncio.Event()

    async def slow_handler(claim: JobClaim) -> None:
        started.set()
        await asyncio.sleep(2.2)

    async with transaction(session_factory) as session:
        session.add(
            Job(
                incident_id=incident_id,
                type=JobType.PROCESS_MESSAGE,
                payload={},
                status=JobStatus.PENDING,
                available_at=NOW - timedelta(seconds=1),
                dedupe_key="heartbeat-test",
            )
        )
    worker = DurableWorker(
        session_factory,
        {JobType.PROCESS_MESSAGE: slow_handler},
        lease_seconds=1,
        worker_id="heartbeat-worker",
    )
    other = DurableWorker(
        session_factory,
        {JobType.PROCESS_MESSAGE: slow_handler},
        lease_seconds=1,
        worker_id="other-worker",
    )

    task = asyncio.create_task(worker.run_once())
    await started.wait()
    await asyncio.sleep(1.2)  # beyond the original 1 s lease
    assert await other.run_once() == 0, "heartbeat must keep the lease owned"
    assert await task == 1

    async with session_factory() as session:
        job = await session.scalar(
            sa.select(Job).where(Job.dedupe_key == "heartbeat-test")
        )
    assert job is not None
    assert job.status is JobStatus.SUCCEEDED
    assert job.attempts == 1


async def test_readiness_reflects_database_and_worker_heartbeat(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+aiosqlite:///:memory:",
        run_worker_in_app=True,
        _env_file=None,
    )
    app = create_app(
        settings, Container(settings=settings, session_factory=session_factory, clock=clock)
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        missing = await client.get("/api/v1/ready")
        assert missing.status_code == 503

        app.state.worker_heartbeat = datetime.now(UTC)
        ready = await client.get("/api/v1/ready")
        assert ready.status_code == 200
        assert ready.json()["worker"] == "ok"

        app.state.worker_heartbeat = datetime.now(UTC) - timedelta(minutes=10)
        stale = await client.get("/api/v1/ready")
        assert stale.status_code == 503


async def test_crash_after_registration_resumes_without_duplicate_effects(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    """Kill the pipeline between registration and matching; resume cleanly.

    The critic fails after the registration transaction has committed
    (message PROCESSED, need persisted). The retry must rebuild matching
    from the committed state — never re-register the need, never reserve
    twice.
    """
    from resilinet.models import Allocation, Entity, Supply
    from resilinet.schemas import EntityType

    harness = await build_recovery_harness(session_factory, incident_id, clock)
    async with transaction(session_factory) as session:
        depot = Entity(
            incident_id=incident_id,
            name="Depot B",
            normalized_name="depot b",
            entity_type=EntityType.DEPOT,
            lat=Decimal("22.572600"),
            lon=Decimal("88.363900"),
            geo_confidence="exact",
        )
        session.add(depot)
        await session.flush()
        session.add(
            Supply(
                incident_id=incident_id,
                entity_id=depot.id,
                resource=Resource.POTABLE_WATER,
                on_hand=Decimal("500"),
                unit="l",
            )
        )
    await accept(harness, "msg-crash", "recovery-need: water please")
    harness.intelligence.fail_next_critique = ProviderUnavailableError(
        "critic down", retryable=True
    )

    assert await harness.worker.run_once() == 1
    async with session_factory() as session:
        from resilinet.models import Message, MessageStatus

        message = await session.scalar(
            sa.select(Message).where(Message.provider_message_id == "msg-crash")
        )
        needs_after_crash = int(
            await session.scalar(
                sa.select(sa.func.count(Need.id)).where(Need.incident_id == incident_id)
            )
            or 0
        )
    assert message is not None and message.status is MessageStatus.PROCESSED
    assert needs_after_crash == 1

    clock.advance(seconds=30)
    await drain(harness)

    job = await job_for(session_factory, incident_id, JobType.PROCESS_MESSAGE)
    assert job is not None and job.status is JobStatus.SUCCEEDED
    async with session_factory() as session:
        needs = int(
            await session.scalar(
                sa.select(sa.func.count(Need.id)).where(Need.incident_id == incident_id)
            )
            or 0
        )
        allocations = int(
            await session.scalar(
                sa.select(sa.func.count(Allocation.id)).where(
                    Allocation.incident_id == incident_id
                )
            )
            or 0
        )
    assert needs == 1, "recovery must not re-register the need"
    assert allocations == 1, "recovery must not double-reserve"


async def test_job_events_carry_timing_without_private_content(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    harness = await build_recovery_harness(session_factory, incident_id, clock)
    await accept(harness, "msg-timing", "recovery-need: Zebra blankets secret body")
    await drain(harness)

    async with session_factory() as session:
        events = list(
            await session.scalars(
                sa.select(EventLog).where(EventLog.incident_id == incident_id)
            )
        )
    succeeded = [e for e in events if e.event_type == "job.succeeded"]
    assert succeeded, "expected a job.succeeded event"
    assert any(
        isinstance(e.payload.get("duration_ms"), (int, float))
        for e in succeeded
        if e.payload.get("job_type") == "process_message"
    )
    for event in events:
        blob = str(event.payload)
        assert "Zebra" not in blob
        assert "volunteer@example.org" not in blob

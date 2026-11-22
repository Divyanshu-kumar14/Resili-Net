from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.adapters.fakes import FakeInboxProvider, provider_unavailable
from resilinet.db import transaction
from resilinet.models import (
    EventLog,
    Message,
    MessageStatus,
    OutboundEmail,
    OutboundStatus,
    ReviewCase,
    Sender,
)
from resilinet.ports import AdapterError
from resilinet.services.notifications import OutboxService

from .conftest import NOW, Clock

PROVIDER_ID = "inbound-1"


async def seed_message(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    inbox: FakeInboxProvider,
    mailbox_id: str,
) -> tuple[Message, Sender]:
    inbox.deliver_inbound(
        mailbox_id=mailbox_id,
        provider_message_id=PROVIDER_ID,
        sender="volunteer@example.org",
        subject="Shelter 4 water",
        text="Shelter 4 needs water.",
        received_at=NOW,
    )
    async with transaction(session_factory) as session:
        sender = Sender(
            incident_id=incident_id,
            address="volunteer@example.org",
            verified=False,
        )
        session.add(sender)
        await session.flush()
        message = Message(
            id=uuid4(),
            incident_id=incident_id,
            sender_id=sender.id,
            provider_message_id=PROVIDER_ID,
            mailbox_id=mailbox_id,
            subject="Shelter 4 water",
            body_text="Shelter 4 needs water.",
            raw_text="Shelter 4 needs water.",
            status=MessageStatus.PROCESSED,
            received_at=NOW,
        )
        session.add(message)
        await session.flush()
        return message, sender


async def make_outbox(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> tuple[OutboxService, FakeInboxProvider, str, Message, Sender]:
    inbox = FakeInboxProvider(webhook_secret="secret", now=clock)
    mailbox = await inbox.provision_inbox("demo-incident")
    message, sender = await seed_message(session_factory, incident_id, inbox, mailbox.id)
    return OutboxService(session_factory, inbox, clock=clock), inbox, mailbox.id, message, sender


async def enqueue_one(
    session_factory: async_sessionmaker[AsyncSession],
    message: Message,
    sender: Sender,
    *,
    kind: str = "recommendation",
    body: str = "Recommendation body.",
) -> UUID:
    async with transaction(session_factory) as session:
        row = await OutboxService.enqueue_reply(
            session,
            message=message,
            sender=sender,
            subject="Re: Shelter 4 water",
            body=body,
            kind=kind,
            available_at=NOW,
        )
        return row.id


async def test_commit_without_send_recovers_and_delivers_exactly_once(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    outbox, inbox, _, message, sender = await make_outbox(session_factory, incident_id, clock)
    row_id = await enqueue_one(session_factory, message, sender)

    # Committed but not yet sent: a fresh worker pass delivers it once.
    assert inbox.sent_messages == ()
    assert await outbox.deliver_due() is True
    assert await outbox.deliver_due() is False

    assert len(inbox.sent_messages) == 1
    async with session_factory() as session:
        row = await session.get(OutboundEmail, row_id)
    assert row is not None and row.status is OutboundStatus.SENT
    assert row.idempotency_key == f"outbox:{row_id}"


async def test_provider_timeout_retries_under_the_same_idempotency_key(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    outbox, inbox, _, message, sender = await make_outbox(session_factory, incident_id, clock)
    row_id = await enqueue_one(session_factory, message, sender)
    inbox.fail_next_send = provider_unavailable()

    assert await outbox.deliver_one(row_id) is True
    async with session_factory() as session:
        row = await session.get(OutboundEmail, row_id)
    assert row is not None and row.status is OutboundStatus.PENDING
    assert row.attempt_count == 1

    clock.advance(seconds=30)
    assert await outbox.deliver_due() is True
    assert len(inbox.sent_messages) == 1
    assert inbox.sent_messages[0].idempotency_key == f"outbox:{row_id}"


async def test_late_worker_cannot_overwrite_a_reclaimed_lease(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    outbox, _inbox, _mailbox, message, sender = await make_outbox(
        session_factory, incident_id, clock
    )
    row_id = await enqueue_one(session_factory, message, sender)

    first = await outbox._claim(row_id)
    assert first is not None
    assert first[0].attempt_count == 1
    async with transaction(session_factory) as session:
        row = await session.get(OutboundEmail, row_id)
        assert row is not None
        row.updated_at = NOW - timedelta(seconds=600)

    second = await outbox._claim(row_id)
    assert second is not None
    assert second[0].attempt_count == 2

    # The first worker finishes late; its attempt number no longer matches.
    await outbox._record_sent(row_id, "late-worker", expected_attempt=1)
    async with session_factory() as session:
        row = await session.get(OutboundEmail, row_id)
        assert row is not None
        assert row.status is OutboundStatus.SENDING
        assert row.provider_message_id is None

    await outbox._record_sent(row_id, "current-worker", expected_attempt=2)
    async with session_factory() as session:
        row = await session.get(OutboundEmail, row_id)
        assert row is not None
        assert row.status is OutboundStatus.SENT
        assert row.provider_message_id == "current-worker"


async def test_two_workers_cannot_deliver_the_same_row_twice(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    outbox, inbox, _, message, sender = await make_outbox(session_factory, incident_id, clock)
    row_id = await enqueue_one(session_factory, message, sender)
    other_worker = OutboxService(session_factory, inbox, clock=clock)

    assert await outbox.deliver_one(row_id) is True
    assert await other_worker.deliver_one(row_id) is False
    assert len(inbox.sent_messages) == 1


async def test_permanent_failure_records_an_event_and_a_review_case(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    outbox, inbox, _, message, sender = await make_outbox(session_factory, incident_id, clock)
    row_id = await enqueue_one(session_factory, message, sender)
    inbox.fail_next_send = AdapterError("provider rejected the message")

    assert await outbox.deliver_one(row_id) is True

    async with session_factory() as session:
        row = await session.get(OutboundEmail, row_id)
        event = await session.scalar(
            sa.select(EventLog).where(EventLog.event_type == "outbox.failed")
        )
        case = await session.scalar(sa.select(ReviewCase))
    assert row is not None and row.status is OutboundStatus.FAILED
    assert event is not None
    assert case is not None
    assert "delivery failed" in case.reason
    assert case.payload["outbox_id"] == str(row_id)
    # A failed row is never silently retried.
    assert await outbox.deliver_due() is False


async def test_lease_expired_final_attempt_fails_with_a_review_case(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    outbox, inbox, _, message, sender = await make_outbox(session_factory, incident_id, clock)
    row_id = await enqueue_one(session_factory, message, sender)
    async with transaction(session_factory) as session:
        row = await session.get(OutboundEmail, row_id)
        assert row is not None
        row.status = OutboundStatus.SENDING
        row.attempt_count = 3
        from datetime import timedelta

        row.updated_at = NOW - timedelta(hours=1)

    await outbox.deliver_due()  # claims the stale row and fails it permanently

    async with session_factory() as session:
        row = await session.get(OutboundEmail, row_id)
        case = await session.scalar(sa.select(ReviewCase))
    assert row is not None and row.status is OutboundStatus.FAILED
    assert case is not None and "delivery failed" in case.reason
    assert inbox.sent_messages == ()


async def test_threaded_reply_preserves_the_provider_message_id(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    outbox, inbox, _, message, sender = await make_outbox(session_factory, incident_id, clock)
    await enqueue_one(session_factory, message, sender)

    assert await outbox.deliver_due() is True

    assert inbox.sent_messages[0].draft.in_reply_to == PROVIDER_ID
    assert inbox.sent_messages[0].draft.to == "volunteer@example.org"


async def test_each_logical_notification_for_one_message_delivers_once(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    outbox, inbox, _, message, sender = await make_outbox(session_factory, incident_id, clock)
    for kind in ("clarification", "recommendation", "verification"):
        await enqueue_one(session_factory, message, sender, kind=kind, body=f"{kind} body")

    while await outbox.deliver_due():
        pass

    assert len(inbox.sent_messages) == 3
    assert len({item.idempotency_key for item in inbox.sent_messages}) == 3
    assert await outbox.deliver_due() is False
    assert len(inbox.sent_messages) == 3


async def test_direct_email_is_sent_not_replied(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    outbox, inbox, mailbox_id, _message, _sender = await make_outbox(
        session_factory, incident_id, clock
    )
    async with transaction(session_factory) as session:
        coordinator = Sender(
            incident_id=incident_id,
            address="coordinator@example.org",
            verified=True,
        )
        session.add(coordinator)
        await session.flush()
        await OutboxService.enqueue_direct(
            session,
            incident_id=incident_id,
            recipient=coordinator,
            mailbox_id=mailbox_id,
            subject="Confirm allocation",
            body="Private confirmation link.",
            kind="confirm_request:AL-000001",
            available_at=NOW,
        )

    assert await outbox.deliver_due() is True

    assert len(inbox.sent_messages) == 1
    assert inbox.sent_messages[0].draft.to == "coordinator@example.org"
    assert inbox.sent_messages[0].draft.in_reply_to is None

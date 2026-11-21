from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.db import transaction
from resilinet.models import (
    Allocation,
    Entity,
    EventLog,
    Message,
    MessageStatus,
    Need,
    OutboundEmail,
    Report,
    ReviewCase,
    ReviewStatus,
    Sender,
    Supply,
)
from resilinet.schemas import (
    ClarificationRequired,
    EntityType,
    FieldConfidence,
    GeoConfidence,
    ParsedEmail,
    ParsedReport,
    ReportContext,
    ReportKind,
    Resource,
    Urgency,
)
from resilinet.services.clarification import (
    ClarificationOutcome,
    ClarificationService,
    ClarificationStateError,
    MergeError,
    ReadyReports,
)
from resilinet.services.notifications import NotificationService

from .conftest import NOW, Clock


def parsed(
    *,
    kind: ReportKind = ReportKind.NEED,
    entity_name: str | None = "Shelter 4",
    entity_type: EntityType = EntityType.SHELTER,
    location_text: str | None = "near Gandhi Park",
    lat: Decimal | None = Decimal("22.572600"),
    lon: Decimal | None = Decimal("88.363900"),
    geo: GeoConfidence = GeoConfidence.EXACT,
    headcount: int | None = 40,
    urgency: Urgency = Urgency.HIGH,
    reports: list[ParsedReport] | None = None,
    split_required: bool = False,
    items_confidence: float = 0.95,
    location_confidence: float = 0.95,
) -> ParsedEmail:
    return ParsedEmail(
        context=ReportContext(
            kind=kind,
            entity_name=entity_name,
            entity_type=entity_type,
            location_text=location_text,
            lat=lat,
            lon=lon,
            geo_confidence=geo,
            headcount=headcount,
            urgency=urgency,
            confidence=FieldConfidence(
                location=location_confidence,
                items=items_confidence,
                headcount=0.95,
            ),
        ),
        reports=reports
        or [ParsedReport(resource=Resource.POTABLE_WATER, quantity=None, unit=None)],
        split_required=split_required,
    )


async def seed_sender(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    *,
    address: str = "volunteer@example.org",
) -> UUID:
    async with transaction(session_factory) as session:
        sender = Sender(incident_id=incident_id, address=address)
        session.add(sender)
        await session.flush()
        return sender.id


async def seed_message(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    sender_id: UUID,
    *,
    provider_message_id: str | None = None,
    subject: str = "Shelter 4 water",
    status: MessageStatus = MessageStatus.QUEUED,
) -> UUID:
    async with transaction(session_factory) as session:
        message = Message(
            incident_id=incident_id,
            sender_id=sender_id,
            provider_message_id=provider_message_id or f"msg-{uuid4()}",
            mailbox_id="mbx-1",
            thread_id="thread-1",
            subject=subject,
            body_text="Shelter 4 has 40 people and zero potable water.",
            raw_text="Shelter 4 has 40 people and zero potable water.",
            status=status,
            received_at=NOW,
        )
        session.add(message)
        await session.flush()
        return message.id


async def test_multi_item_supply_for_one_entity_is_ready(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    sender_id = await seed_sender(session_factory, incident_id)
    message_id = await seed_message(session_factory, incident_id, sender_id)
    supply = parsed(
        kind=ReportKind.SUPPLY,
        entity_name="Depot B",
        entity_type=EntityType.DEPOT,
        headcount=None,
        urgency=Urgency.UNKNOWN,
        reports=[
            ParsedReport(resource=Resource.POTABLE_WATER, quantity=Decimal("300"), unit="gallons"),
            ParsedReport(resource=Resource.FOOD, quantity=Decimal("200"), unit="meals"),
        ],
    )

    async with transaction(session_factory) as session:
        message = await session.get(Message, message_id)
        assert message is not None
        service = ClarificationService(session, NotificationService(), clock=lambda: NOW)
        decision = service.evaluate(supply, message)
        assert isinstance(decision, ReadyReports)
        result = await service.process_ready(message, supply)

    assert result.entity_id is not None
    async with session_factory() as session:
        reports = list(await session.scalars(sa.select(Report)))
        entity = await session.get(Entity, result.entity_id)
        message = await session.get(Message, message_id)
    assert len(reports) == 2
    assert {report.resource for report in reports} == {
        Resource.POTABLE_WATER,
        Resource.FOOD,
    }
    assert entity is not None and entity.name == "Depot B"
    assert message is not None and message.status is MessageStatus.PROCESSED


async def test_missing_location_asks_exactly_one_question(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    sender_id = await seed_sender(session_factory, incident_id)
    message_id = await seed_message(session_factory, incident_id, sender_id)
    unlocated = parsed(location_text=None, lat=None, lon=None, geo=GeoConfidence.NONE)

    async with transaction(session_factory) as session:
        message = await session.get(Message, message_id)
        assert message is not None
        service = ClarificationService(session, NotificationService(), clock=clock)
        decision = service.evaluate(unlocated, message)
        assert isinstance(decision, ClarificationOutcome)
        assert decision.fields == ("location",)
        await service.ask(message, decision, unlocated)

    async with session_factory() as session:
        message = await session.get(Message, message_id)
        notices = list(await session.scalars(sa.select(OutboundEmail)))
        entity_count = await session.scalar(sa.select(sa.func.count(Entity.id)))
    assert message is not None
    assert message.status is MessageStatus.CLARIFICATION_PENDING
    assert message.clarification_fields == ["location"]
    assert message.clarification_attempts == 1
    assert message.parsed_json is not None
    assert len(notices) == 1
    assert notices[0].kind == "clarification"
    assert "location" in notices[0].body_text
    assert notices[0].available_at == clock.now
    assert entity_count == 0


async def test_missing_quantity_and_headcount_is_one_question(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    sender_id = await seed_sender(session_factory, incident_id)
    message_id = await seed_message(session_factory, incident_id, sender_id)
    ambiguous = parsed(headcount=None)

    async with transaction(session_factory) as session:
        message = await session.get(Message, message_id)
        assert message is not None
        service = ClarificationService(session, NotificationService(), clock=lambda: NOW)
        decision = service.evaluate(ambiguous, message)
        assert isinstance(decision, ClarificationOutcome)
        assert set(decision.fields) == {"quantity", "headcount"}
        await service.ask(message, decision, ambiguous)

    async with session_factory() as session:
        notices = list(await session.scalars(sa.select(OutboundEmail)))
    assert len(notices) == 1
    assert "quantity" in notices[0].body_text
    assert "headcount" in notices[0].body_text


async def test_estimable_missing_quantity_needs_no_clarification(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    sender_id = await seed_sender(session_factory, incident_id)
    message_id = await seed_message(session_factory, incident_id, sender_id)

    async with transaction(session_factory) as session:
        message = await session.get(Message, message_id)
        assert message is not None
        decision = ClarificationService(
            session, NotificationService(), clock=lambda: NOW
        ).evaluate(parsed(headcount=40), message)
    assert isinstance(decision, ReadyReports)


async def test_mixed_entities_or_locations_requests_a_split(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    sender_id = await seed_sender(session_factory, incident_id)
    message_id = await seed_message(session_factory, incident_id, sender_id)
    mixed = parsed(split_required=True)

    async with transaction(session_factory) as session:
        message = await session.get(Message, message_id)
        assert message is not None
        service = ClarificationService(session, NotificationService(), clock=lambda: NOW)
        decision = service.evaluate(mixed, message)
        assert isinstance(decision, ClarificationOutcome)
        assert decision.fields == ("split",)
        assert "more than one place" in decision.question
        await service.ask(message, decision, mixed)

    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count(Report.id))) == 0
        assert await session.scalar(sa.select(sa.func.count(Entity.id))) == 0
        notice = await session.scalar(sa.select(OutboundEmail))
    assert notice is not None and "one message per place" in notice.body_text


async def test_valid_correction_merges_only_the_missing_fields(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    sender_id = await seed_sender(session_factory, incident_id)
    original_id = await seed_message(
        session_factory, incident_id, sender_id, provider_message_id="msg-orig"
    )
    correction_id = await seed_message(
        session_factory,
        incident_id,
        sender_id,
        provider_message_id="msg-correction",
        status=MessageStatus.CLARIFICATION_PENDING,
    )
    original_parsed = parsed(headcount=None)
    correction = parsed(
        headcount=40,
        reports=[
            ParsedReport(resource=Resource.POTABLE_WATER, quantity=Decimal("120"), unit="l")
        ],
    )

    async with transaction(session_factory) as session:
        original = await session.get(Message, original_id)
        correction_row = await session.get(Message, correction_id)
        assert original is not None and correction_row is not None
        original.status = MessageStatus.CLARIFICATION_PENDING
        original.clarification_fields = ["quantity", "headcount"]
        original.clarification_attempts = 1
        original.parsed_json = original_parsed.model_dump(mode="json")
        correction_row.reply_to_message_id = original_id
        service = ClarificationService(session, NotificationService(), clock=lambda: NOW)
        await service.handle_correction(original, correction_row, correction)

    async with session_factory() as session:
        original = await session.get(Message, original_id)
        correction_row = await session.get(Message, correction_id)
        reports = list(await session.scalars(sa.select(Report)))
        notice = await session.scalar(sa.select(OutboundEmail))
        merged_event = await session.scalar(
            sa.select(EventLog).where(EventLog.event_type == "clarification.merged")
        )
    assert original is not None and original.status is MessageStatus.PROCESSED
    assert correction_row is not None and correction_row.status is MessageStatus.PROCESSED
    assert len(reports) == 1
    assert reports[0].quantity == Decimal("120.000")
    assert reports[0].unit == "l"
    assert reports[0].headcount == 40
    assert notice is not None and notice.kind == "clarification_merged"
    assert merged_event is not None


async def test_unasked_fields_are_not_overwritten(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    sender_id = await seed_sender(session_factory, incident_id)
    await seed_message(session_factory, incident_id, sender_id)
    original = parsed(
        location_text=None,
        lat=None,
        lon=None,
        geo=GeoConfidence.NONE,
        reports=[
            ParsedReport(resource=Resource.POTABLE_WATER, quantity=Decimal("100"), unit="l")
        ],
    )
    correction = parsed(
        location_text="near Gandhi Park",
        reports=[
            ParsedReport(resource=Resource.POTABLE_WATER, quantity=Decimal("5000"), unit="l")
        ],
    )

    async with transaction(session_factory) as session:
        service = ClarificationService(session, NotificationService(), clock=lambda: NOW)
        merged = service.merge(original, correction, ["location"])

    assert merged.reports[0].quantity == Decimal("100")
    assert merged.context.lat == Decimal("22.572600")
    assert merged.context.geo_confidence is GeoConfidence.EXACT


async def test_exact_correction_overrides_low_model_confidence(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
) -> None:
    sender_id = await seed_sender(session_factory, incident_id)
    await seed_message(session_factory, incident_id, sender_id)
    original = parsed(
        location_text=None,
        lat=None,
        lon=None,
        geo=GeoConfidence.NONE,
    )
    correction = parsed(location_confidence=0.5)

    async with transaction(session_factory) as session:
        service = ClarificationService(
            session, NotificationService(), clock=lambda: NOW
        )
        merged = service.merge(original, correction, ["location"])

    assert merged.context.lat == Decimal("22.572600")
    assert merged.context.geo_confidence is GeoConfidence.EXACT


async def test_merging_an_unresolvable_field_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        service = ClarificationService(session, NotificationService(), clock=lambda: NOW)
        with pytest.raises(MergeError):
            service.merge(parsed(), parsed(), ["split"])


async def test_a_second_ambiguity_escalates_to_human_review(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    sender_id = await seed_sender(session_factory, incident_id)
    original_id = await seed_message(session_factory, incident_id, sender_id)
    correction_id = await seed_message(session_factory, incident_id, sender_id)
    original_parsed = parsed(headcount=None)
    still_ambiguous = parsed(headcount=None)

    async with transaction(session_factory) as session:
        original = await session.get(Message, original_id)
        correction_row = await session.get(Message, correction_id)
        assert original is not None and correction_row is not None
        original.status = MessageStatus.CLARIFICATION_PENDING
        original.clarification_fields = ["quantity", "headcount"]
        original.parsed_json = original_parsed.model_dump(mode="json")
        service = ClarificationService(session, NotificationService(), clock=lambda: NOW)
        await service.handle_correction(original, correction_row, still_ambiguous)

    async with session_factory() as session:
        original = await session.get(Message, original_id)
        correction_row = await session.get(Message, correction_id)
        review = await session.scalar(sa.select(ReviewCase))
        notice = await session.scalar(sa.select(OutboundEmail))
        reported = await session.scalar(sa.select(sa.func.count(Report.id)))
        event = await session.scalar(
            sa.select(EventLog).where(EventLog.event_type == "clarification.escalated")
        )
    assert original is not None and original.status is MessageStatus.NEEDS_REVIEW
    assert correction_row is not None and correction_row.status is MessageStatus.NEEDS_REVIEW
    assert review is not None and review.status is ReviewStatus.OPEN
    assert notice is not None and notice.kind == "clarification_review"
    assert reported == 0
    assert event is not None


async def test_a_wrong_sender_correction_is_rejected_and_not_merged(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    original_sender = await seed_sender(
        session_factory, incident_id, address="volunteer@example.org"
    )
    other_sender = await seed_sender(
        session_factory, incident_id, address="stranger@example.org"
    )
    original_id = await seed_message(session_factory, incident_id, original_sender)
    correction_id = await seed_message(session_factory, incident_id, other_sender)
    original_parsed = parsed(headcount=None)
    correction = parsed(
        reports=[
            ParsedReport(resource=Resource.POTABLE_WATER, quantity=Decimal("120"), unit="l")
        ]
    )

    async with transaction(session_factory) as session:
        original = await session.get(Message, original_id)
        correction_row = await session.get(Message, correction_id)
        assert original is not None and correction_row is not None
        original.status = MessageStatus.CLARIFICATION_PENDING
        original.clarification_fields = ["quantity", "headcount"]
        original.parsed_json = original_parsed.model_dump(mode="json")
        service = ClarificationService(session, NotificationService(), clock=lambda: NOW)
        await service.handle_correction(original, correction_row, correction)

    async with session_factory() as session:
        original = await session.get(Message, original_id)
        correction_row = await session.get(Message, correction_id)
        notice = await session.scalar(sa.select(OutboundEmail))
        review = await session.scalar(sa.select(ReviewCase))
        reported = await session.scalar(sa.select(sa.func.count(Report.id)))
    assert original is not None and original.status is MessageStatus.CLARIFICATION_PENDING
    assert correction_row is not None and correction_row.status is MessageStatus.NEEDS_REVIEW
    assert review is not None
    assert notice is not None
    assert notice.to_address == "stranger@example.org"
    assert notice.sender_id == other_sender
    assert "different email address" in notice.body_text
    assert reported == 0


async def test_no_stock_or_need_is_created_by_clarification(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    sender_id = await seed_sender(session_factory, incident_id)
    message_id = await seed_message(session_factory, incident_id, sender_id)

    async with transaction(session_factory) as session:
        message = await session.get(Message, message_id)
        assert message is not None
        service = ClarificationService(session, NotificationService(), clock=lambda: NOW)
        await service.process_ready(message, parsed())

    async with session_factory() as session:
        for model in (Need, Supply, Allocation):
            count = await session.scalar(sa.select(sa.func.count()).select_from(model))
            assert count == 0


async def test_a_second_correction_for_one_report_escalates(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    sender_id = await seed_sender(session_factory, incident_id)
    original_id = await seed_message(session_factory, incident_id, sender_id)
    first_correction_id = await seed_message(session_factory, incident_id, sender_id)
    second_correction_id = await seed_message(session_factory, incident_id, sender_id)
    original_parsed = parsed(headcount=None)
    correction = parsed(
        reports=[
            ParsedReport(resource=Resource.POTABLE_WATER, quantity=Decimal("120"), unit="l")
        ]
    )

    async with transaction(session_factory) as session:
        original = await session.get(Message, original_id)
        first_correction = await session.get(Message, first_correction_id)
        second_correction = await session.get(Message, second_correction_id)
        assert original is not None
        assert first_correction is not None and second_correction is not None
        original.status = MessageStatus.CLARIFICATION_PENDING
        original.clarification_fields = ["quantity", "headcount"]
        original.parsed_json = original_parsed.model_dump(mode="json")
        first_correction.reply_to_message_id = original_id
        second_correction.reply_to_message_id = original_id
        await session.flush()
        service = ClarificationService(session, NotificationService(), clock=lambda: NOW)
        await service.handle_correction(original, second_correction, correction)

    async with session_factory() as session:
        original = await session.get(Message, original_id)
        review = await session.scalar(sa.select(ReviewCase))
        event = await session.scalar(
            sa.select(EventLog).where(
                EventLog.event_type == "clarification.second_correction"
            )
        )
    assert original is not None and original.status is MessageStatus.NEEDS_REVIEW
    assert review is not None
    assert event is not None


async def test_partial_multi_resource_need_asks_for_the_missing_quantity(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    sender_id = await seed_sender(session_factory, incident_id)
    message_id = await seed_message(session_factory, incident_id, sender_id)
    partial = parsed(
        headcount=None,
        reports=[
            ParsedReport(
                resource=Resource.POTABLE_WATER, quantity=Decimal("50"), unit="l"
            ),
            ParsedReport(resource=Resource.FOOD, quantity=None, unit=None),
        ],
    )

    async with transaction(session_factory) as session:
        message = await session.get(Message, message_id)
        assert message is not None
        decision = ClarificationService(
            session, NotificationService(), clock=lambda: NOW
        ).evaluate(partial, message)

    assert isinstance(decision, ClarificationOutcome)
    assert "quantity" in decision.fields
    assert "headcount" in decision.fields


async def test_model_item_field_is_asked_as_a_quantity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        service = ClarificationService(session, NotificationService(), clock=lambda: NOW)
        outcome = service.outcome_for_required(
            ClarificationRequired(fields=["items"], kind=ReportKind.NEED)
        )
    assert outcome.fields == ("quantity",)
    assert "quantity" in outcome.question


async def test_a_completed_report_cannot_be_recompleted(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    sender_id = await seed_sender(session_factory, incident_id)
    message_id = await seed_message(session_factory, incident_id, sender_id)

    async with transaction(session_factory) as session:
        message = await session.get(Message, message_id)
        assert message is not None
        service = ClarificationService(session, NotificationService(), clock=lambda: NOW)
        await service.process_ready(message, parsed())

    async with transaction(session_factory) as session:
        message = await session.get(Message, message_id)
        assert message is not None
        service = ClarificationService(session, NotificationService(), clock=lambda: NOW)
        with pytest.raises(ClarificationStateError):
            await service.process_ready(message, parsed())


async def test_a_late_correction_never_regresses_a_completed_report(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    sender_id = await seed_sender(session_factory, incident_id)
    original_id = await seed_message(session_factory, incident_id, sender_id)
    correction_id = await seed_message(session_factory, incident_id, sender_id)

    async with transaction(session_factory) as session:
        original = await session.get(Message, original_id)
        assert original is not None
        service = ClarificationService(session, NotificationService(), clock=lambda: NOW)
        await service.process_ready(original, parsed())

    async with transaction(session_factory) as session:
        original = await session.get(Message, original_id)
        correction = await session.get(Message, correction_id)
        assert original is not None and correction is not None
        correction.reply_to_message_id = original_id
        service = ClarificationService(session, NotificationService(), clock=lambda: NOW)
        await service.handle_correction(original, correction, parsed())

    async with session_factory() as session:
        original = await session.get(Message, original_id)
        correction = await session.get(Message, correction_id)
        reports = await session.scalar(sa.select(sa.func.count(Report.id)))
        review = await session.scalar(sa.select(ReviewCase))
        event = await session.scalar(
            sa.select(EventLog).where(
                EventLog.event_type == "clarification.late_correction"
            )
        )
    assert original is not None and original.status is MessageStatus.PROCESSED
    assert correction is not None and correction.status is MessageStatus.NEEDS_REVIEW
    assert reports == 1
    assert review is not None
    assert event is not None


async def test_an_implausible_correction_quantity_is_escalated(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    sender_id = await seed_sender(session_factory, incident_id)
    original_id = await seed_message(session_factory, incident_id, sender_id)
    correction_id = await seed_message(session_factory, incident_id, sender_id)
    original_parsed = parsed(headcount=None)
    implausible = parsed(
        headcount=40,
        reports=[
            ParsedReport(
                resource=Resource.POTABLE_WATER,
                quantity=Decimal("2000000"),
                unit="l",
            )
        ],
    )

    async with transaction(session_factory) as session:
        original = await session.get(Message, original_id)
        correction = await session.get(Message, correction_id)
        assert original is not None and correction is not None
        original.status = MessageStatus.CLARIFICATION_PENDING
        original.clarification_fields = ["quantity", "headcount"]
        original.parsed_json = original_parsed.model_dump(mode="json")
        service = ClarificationService(session, NotificationService(), clock=lambda: NOW)
        await service.handle_correction(original, correction, implausible)

    async with session_factory() as session:
        original = await session.get(Message, original_id)
        review = await session.scalar(sa.select(ReviewCase))
        reports = await session.scalar(sa.select(sa.func.count(Report.id)))
    assert original is not None and original.status is MessageStatus.NEEDS_REVIEW
    assert review is not None
    assert reports == 0


async def test_a_low_confidence_correction_is_escalated(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    sender_id = await seed_sender(session_factory, incident_id)
    original_id = await seed_message(session_factory, incident_id, sender_id)
    correction_id = await seed_message(session_factory, incident_id, sender_id)
    original_parsed = parsed(headcount=None)
    uncertain = parsed(
        headcount=40,
        items_confidence=0.5,
        reports=[
            ParsedReport(
                resource=Resource.POTABLE_WATER, quantity=Decimal("120"), unit="l"
            )
        ],
    )

    async with transaction(session_factory) as session:
        original = await session.get(Message, original_id)
        correction = await session.get(Message, correction_id)
        assert original is not None and correction is not None
        original.status = MessageStatus.CLARIFICATION_PENDING
        original.clarification_fields = ["quantity", "headcount"]
        original.parsed_json = original_parsed.model_dump(mode="json")
        service = ClarificationService(session, NotificationService(), clock=lambda: NOW)
        await service.handle_correction(original, correction, uncertain)

    async with session_factory() as session:
        original = await session.get(Message, original_id)
        review = await session.scalar(sa.select(ReviewCase))
    assert original is not None and original.status is MessageStatus.NEEDS_REVIEW
    assert review is not None

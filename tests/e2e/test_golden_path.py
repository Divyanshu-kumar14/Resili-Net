import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from resilinet.adapters.agentboxd import sign_webhook
from resilinet.adapters.fakes import FakeGeocoder, FakeInboxProvider, FakeIntelligenceProvider
from resilinet.ports import ProviderUnavailableError
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
from resilinet.services.pipeline import Pipeline

NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
INCIDENT_ID = UUID("00000000-0000-0000-0000-000000000001")
WEBHOOK_SECRET = "test-secret"
AGENTBOXD_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "agentboxd"


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def need_fixture() -> ParsedEmail:
    return ParsedEmail(
        context=ReportContext(
            kind=ReportKind.NEED,
            entity_name="Shelter 4",
            entity_type=EntityType.SHELTER,
            location_text="near Gandhi Park",
            headcount=40,
            urgency=Urgency.HIGH,
            confidence=FieldConfidence(location=0.95, items=0.92, headcount=0.97),
        ),
        reports=[
            ParsedReport(
                resource=Resource.POTABLE_WATER,
                quantity=None,
                unit=None,
                qualifier="zero available",
            )
        ],
    )


def supply_fixture() -> ParsedEmail:
    return ParsedEmail(
        context=ReportContext(
            kind=ReportKind.SUPPLY,
            entity_name="Depot B",
            entity_type=EntityType.DEPOT,
            location_text="on River Road",
            confidence=FieldConfidence(location=0.95, items=0.97, headcount=0.9),
        ),
        reports=[
            ParsedReport(
                resource=Resource.POTABLE_WATER,
                quantity=Decimal("300"),
                unit="gallons",
            )
        ],
    )


async def test_need_email_to_recommendation_reply_and_map() -> None:
    clock = Clock(NOW)
    inbox = FakeInboxProvider(webhook_secret="test-secret", now=clock)
    mailbox = await inbox.provision_inbox("demo-incident")
    inbox.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-supply",
        sender="depot-b@example.org",
        subject="Depot B stock",
        text="Depot B has 300 gallons of potable water on River Road.",
        thread_id="thread-supply",
        received_at=NOW,
    )
    inbox.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-need",
        sender="volunteer@example.org",
        subject="Shelter 4 water",
        text="Shelter 4 has 40 people and zero potable water. Near Gandhi Park.",
        thread_id="thread-need",
        received_at=NOW,
    )
    pipeline = Pipeline(
        incident_id=INCIDENT_ID,
        inbox=inbox,
        intelligence=FakeIntelligenceProvider(
            {
                "Shelter 4": need_fixture(),
                "Depot B": supply_fixture(),
            }
        ),
        geocoder=FakeGeocoder(
            {
                "gandhi park": (Decimal("18.520400"), Decimal("73.856700")),
                "river road": (Decimal("18.531000"), Decimal("73.871000")),
            }
        ),
        clock=clock,
    )

    supply_result = await pipeline.process(await inbox.fetch_message("msg-supply"))
    result = await pipeline.process(await inbox.fetch_message("msg-need"))

    assert supply_result.public_projection["supplies"][0]["on_hand"] == "1135.500"

    assert result.need_ref is not None
    assert result.need_ref.startswith("RN-")
    assert result.allocation_ref is not None
    assert result.allocation_ref.startswith("AL-")

    projection = result.public_projection
    need = projection["needs"][0]
    assert need["quantity"] == "120.000"
    assert need["unit"] == "l"
    assert need["quantity_source"] == "estimated"
    assert need["status"] == "matched"

    supply = projection["supplies"][0]
    assert supply["on_hand"] == "1135.500"
    assert supply["available"] == "1015.500"

    allocation = projection["allocations"][0]
    assert allocation["ref"] == result.allocation_ref
    assert allocation["need_ref"] == result.need_ref
    assert allocation["quantity"] == "120.000"
    assert allocation["status"] == "proposed"

    entity_names = {entity["name"] for entity in projection["entities"]}
    assert entity_names == {"Shelter 4", "Depot B"}

    assert result.reply is not None
    reply_text = result.reply.text
    lowered = reply_text.lower()
    assert "recommended" in lowered
    assert "depot b" in lowered
    assert "estimated" in lowered
    assert "~120 l" in lowered
    assert result.need_ref in reply_text
    assert "dispatched" not in lowered
    assert "delivered" not in lowered

    assert result.sent is not None
    need_replies = [
        delivered
        for delivered in inbox.sent_messages
        if delivered.draft.in_reply_to == "msg-need"
    ]
    assert len(need_replies) == 1
    sent = need_replies[0]
    assert sent.draft.in_reply_to == "msg-need"
    assert sent.idempotency_key.startswith("outbox:")

    serialized = json.dumps(projection)
    assert "volunteer@example.org" not in serialized
    assert "depot-b@example.org" not in serialized
    assert "msg-need" not in serialized
    assert "msg-supply" not in serialized


async def test_zero_stated_need_is_treated_as_missing_and_estimated() -> None:
    clock = Clock(NOW)
    inbox = FakeInboxProvider(webhook_secret="test-secret", now=clock)
    mailbox = await inbox.provision_inbox("demo-incident")
    inbox.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-zero-need",
        sender="volunteer2@example.org",
        subject="Shelter 7 water",
        text="Shelter 7 has 20 people and 0 litres of potable water. Near Gandhi Park.",
        received_at=NOW,
    )
    zero_need = ParsedEmail(
        context=ReportContext(
            kind=ReportKind.NEED,
            entity_name="Shelter 7",
            entity_type=EntityType.SHELTER,
            location_text="near Gandhi Park",
            headcount=20,
            urgency=Urgency.HIGH,
            confidence=FieldConfidence(location=0.95, items=0.92, headcount=0.97),
        ),
        reports=[
            ParsedReport(
                resource=Resource.POTABLE_WATER,
                quantity=Decimal("0"),
                unit="l",
                qualifier="zero available",
            )
        ],
    )
    pipeline = Pipeline(
        incident_id=INCIDENT_ID,
        inbox=inbox,
        intelligence=FakeIntelligenceProvider({"Shelter 7": zero_need}),
        geocoder=FakeGeocoder(
            {"gandhi park": (Decimal("18.520400"), Decimal("73.856700"))}
        ),
        clock=clock,
    )

    result = await pipeline.process(await inbox.fetch_message("msg-zero-need"))

    need = result.public_projection["needs"][0]
    assert need["quantity"] == "60.000"
    assert need["quantity_source"] == "estimated"
    assert need["status"] == "open"
    assert result.allocation_ref is None
    assert result.reply is not None
    assert "No stock is currently reserved" in result.reply.text


async def test_retry_uses_the_same_outbox_key_and_does_not_duplicate_email() -> None:
    clock = Clock(NOW)
    inbox = FakeInboxProvider(webhook_secret=WEBHOOK_SECRET, now=clock)
    mailbox = await inbox.provision_inbox("demo-incident")
    inbox.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-supply",
        sender="depot-b@example.org",
        subject="Depot B stock",
        text="Depot B has 300 gallons of potable water on River Road.",
        received_at=NOW,
    )
    inbox.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-need",
        sender="volunteer@example.org",
        subject="Shelter 4 water",
        text="Shelter 4 has 40 people and zero potable water. Near Gandhi Park.",
        received_at=NOW,
    )

    def build() -> Pipeline:
        return Pipeline(
            incident_id=INCIDENT_ID,
            inbox=inbox,
            intelligence=FakeIntelligenceProvider(
                {"Shelter 4": need_fixture(), "Depot B": supply_fixture()}
            ),
            geocoder=FakeGeocoder(
                {
                    "gandhi park": (Decimal("18.520400"), Decimal("73.856700")),
                    "river road": (Decimal("18.531000"), Decimal("73.871000")),
                }
            ),
            clock=clock,
        )

    first = build()
    await first.process(await inbox.fetch_message("msg-supply"))
    await first.process(await inbox.fetch_message("msg-need"))
    assert len(inbox.sent_messages) == 2

    retry = build()
    await retry.process(await inbox.fetch_message("msg-need"))

    assert len(inbox.sent_messages) == 2


async def test_failed_send_is_retried_without_duplicate_domain_state() -> None:
    clock = Clock(NOW)
    inbox = FakeInboxProvider(webhook_secret=WEBHOOK_SECRET, now=clock)
    mailbox = await inbox.provision_inbox("demo-incident")
    inbox.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-need",
        sender="volunteer@example.org",
        subject="Shelter 4 water",
        text="Shelter 4 has 40 people and zero potable water. Near Gandhi Park.",
        received_at=NOW,
    )
    pipeline = Pipeline(
        incident_id=INCIDENT_ID,
        inbox=inbox,
        intelligence=FakeIntelligenceProvider({"Shelter 4": need_fixture()}),
        geocoder=FakeGeocoder(
            {"gandhi park": (Decimal("18.520400"), Decimal("73.856700"))}
        ),
        clock=clock,
    )
    inbox.fail_next_send = ProviderUnavailableError("smtp down")

    with pytest.raises(ProviderUnavailableError):
        await pipeline.process(await inbox.fetch_message("msg-need"))

    assert len(pipeline.projection()["needs"]) == 1

    result = await pipeline.process(await inbox.fetch_message("msg-need"))

    assert result.sent is not None
    assert len(pipeline.projection()["needs"]) == 1
    assert len(inbox.sent_messages) == 1


async def test_supply_snapshot_below_reservations_is_rejected_without_mutation() -> None:
    clock = Clock(NOW)
    inbox = FakeInboxProvider(webhook_secret=WEBHOOK_SECRET, now=clock)
    mailbox = await inbox.provision_inbox("demo-incident")
    for message_id, text in (
        ("msg-supply-100", "Depot B has 100 litres of potable water on River Road."),
        (
            "msg-need",
            "Shelter 4 has 40 people and zero potable water. Near Gandhi Park.",
        ),
        ("msg-supply-50", "Depot B has 50 litres of potable water on River Road."),
    ):
        inbox.deliver_inbound(
            mailbox_id=mailbox.id,
            provider_message_id=message_id,
            sender="sender@example.org",
            subject="Relief report",
            text=text,
            received_at=NOW,
        )

    supply_100 = supply_fixture().model_copy(
        update={
            "reports": [
                ParsedReport(
                    resource=Resource.POTABLE_WATER,
                    quantity=Decimal("100"),
                    unit="l",
                )
            ]
        }
    )
    supply_50 = supply_fixture().model_copy(
        update={
            "reports": [
                ParsedReport(
                    resource=Resource.POTABLE_WATER,
                    quantity=Decimal("50"),
                    unit="l",
                )
            ]
        }
    )
    pipeline = Pipeline(
        incident_id=INCIDENT_ID,
        inbox=inbox,
        intelligence=FakeIntelligenceProvider(
            {
                "100 litres": supply_100,
                "50 litres": supply_50,
                "Shelter 4": need_fixture(),
            }
        ),
        geocoder=FakeGeocoder(
            {
                "gandhi park": (Decimal("18.520400"), Decimal("73.856700")),
                "river road": (Decimal("18.531000"), Decimal("73.871000")),
            }
        ),
        clock=clock,
    )

    await pipeline.process(await inbox.fetch_message("msg-supply-100"))
    await pipeline.process(await inbox.fetch_message("msg-need"))
    result = await pipeline.process(await inbox.fetch_message("msg-supply-50"))

    projection = result.public_projection
    assert projection["supplies"][0]["on_hand"] == "100.000"
    assert projection["supplies"][0]["available"] == "0.000"
    assert result.reply is not None
    assert "not applied" in result.reply.text.lower()
    assert len(inbox.sent_messages) == 3


async def test_compound_need_is_clarified_before_any_need_is_persisted() -> None:
    clock = Clock(NOW)
    inbox = FakeInboxProvider(webhook_secret=WEBHOOK_SECRET, now=clock)
    mailbox = await inbox.provision_inbox("demo-incident")
    inbox.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-compound",
        sender="volunteer@example.org",
        subject="Shelter 4 supplies",
        text="Shelter 4 needs water and medicine. Near Gandhi Park.",
        received_at=NOW,
    )
    compound = ParsedEmail(
        context=ReportContext(
            kind=ReportKind.NEED,
            entity_name="Shelter 4",
            entity_type=EntityType.SHELTER,
            location_text="near Gandhi Park",
            headcount=40,
            urgency=Urgency.HIGH,
            confidence=FieldConfidence(location=0.95, items=0.92, headcount=0.97),
        ),
        reports=[
            ParsedReport(
                resource=Resource.POTABLE_WATER,
                quantity=Decimal("10"),
                unit="l",
            ),
            ParsedReport(
                resource=Resource.MEDICINE,
                quantity=Decimal("5"),
                unit="doses",
            ),
        ],
    )
    pipeline = Pipeline(
        incident_id=INCIDENT_ID,
        inbox=inbox,
        intelligence=FakeIntelligenceProvider({"Shelter 4": compound}),
        geocoder=FakeGeocoder(
            {"gandhi park": (Decimal("18.520400"), Decimal("73.856700"))}
        ),
        clock=clock,
    )

    result = await pipeline.process(await inbox.fetch_message("msg-compound"))

    assert result.clarification_question is not None
    assert result.kind is ReportKind.NEED
    assert result.public_projection["needs"] == []
    assert len(inbox.sent_messages) == 1


async def test_unlocatable_need_clarification_keeps_the_original_kind() -> None:
    clock = Clock(NOW)
    inbox = FakeInboxProvider(webhook_secret=WEBHOOK_SECRET, now=clock)
    mailbox = await inbox.provision_inbox("demo-incident")
    inbox.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-unknown-place",
        sender="volunteer@example.org",
        subject="Water",
        text="Shelter 4 has 40 people and zero potable water. Near Atlantis.",
        received_at=NOW,
    )
    unlocatable = need_fixture().model_copy(
        update={
            "context": need_fixture().context.model_copy(
                update={"location_text": "near Atlantis"}
            )
        }
    )
    pipeline = Pipeline(
        incident_id=INCIDENT_ID,
        inbox=inbox,
        intelligence=FakeIntelligenceProvider({"Shelter 4": unlocatable}),
        geocoder=FakeGeocoder({}),
        clock=clock,
    )

    result = await pipeline.process(await inbox.fetch_message("msg-unknown-place"))

    assert result.kind is ReportKind.NEED
    assert result.clarification_question is not None
    assert result.public_projection["needs"] == []


async def test_duplicate_provider_events_create_one_need_and_one_reply() -> None:
    clock = Clock(NOW)
    inbox = FakeInboxProvider(webhook_secret=WEBHOOK_SECRET, now=clock)
    pipeline = Pipeline(
        incident_id=INCIDENT_ID,
        inbox=inbox,
        intelligence=FakeIntelligenceProvider({"Shelter 4": need_fixture()}),
        geocoder=FakeGeocoder(
            {"gandhi park": (Decimal("18.520400"), Decimal("73.856700"))}
        ),
        clock=clock,
    )

    def signed_event(name: str) -> tuple[bytes, dict[str, str]]:
        payload = json.loads((AGENTBOXD_FIXTURES / name).read_text())
        raw = json.dumps(payload, separators=(",", ":")).encode()
        timestamp = int(NOW.timestamp())
        return raw, {
            "X-Mailroom-Timestamp": str(timestamp),
            "X-Mailroom-Signature": sign_webhook(raw, WEBHOOK_SECRET, timestamp),
        }

    for name in (
        "webhook_received_full.json",
        "webhook_enriched.json",
        "webhook_released.json",
    ):
        event = await inbox.parse_webhook(*signed_event(name))
        assert event.raw is not None
        await pipeline.process(event.raw)

    projection = pipeline.projection()
    assert len(projection["needs"]) == 1
    assert len(inbox.sent_messages) == 1


async def test_partial_match_reply_states_the_partial_quantity() -> None:
    clock = Clock(NOW)
    inbox = FakeInboxProvider(webhook_secret=WEBHOOK_SECRET, now=clock)
    mailbox = await inbox.provision_inbox("demo-incident")
    inbox.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-supply-50",
        sender="depot-b@example.org",
        subject="Depot B stock",
        text="Depot B has 50 litres of potable water on River Road.",
        received_at=NOW,
    )
    inbox.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-need",
        sender="volunteer@example.org",
        subject="Shelter 4 water",
        text="Shelter 4 has 40 people and zero potable water. Near Gandhi Park.",
        received_at=NOW,
    )
    supply_50 = supply_fixture().model_copy(
        update={
            "reports": [
                ParsedReport(
                    resource=Resource.POTABLE_WATER,
                    quantity=Decimal("50"),
                    unit="l",
                )
            ]
        }
    )
    pipeline = Pipeline(
        incident_id=INCIDENT_ID,
        inbox=inbox,
        intelligence=FakeIntelligenceProvider(
            {"50 litres": supply_50, "Shelter 4": need_fixture()}
        ),
        geocoder=FakeGeocoder(
            {
                "gandhi park": (Decimal("18.520400"), Decimal("73.856700")),
                "river road": (Decimal("18.531000"), Decimal("73.871000")),
            }
        ),
        clock=clock,
    )

    await pipeline.process(await inbox.fetch_message("msg-supply-50"))
    result = await pipeline.process(await inbox.fetch_message("msg-need"))

    allocation = result.public_projection["allocations"][0]
    assert allocation["quantity"] == "50.000"
    assert result.public_projection["needs"][0]["status"] == "partial"
    assert result.reply is not None
    lowered = result.reply.text.lower()
    assert "~50 l" in lowered
    assert "partial match" in lowered
    assert "~120 l" in lowered


@pytest.mark.parametrize("kind", [ReportKind.NEED, ReportKind.SUPPLY])
@pytest.mark.parametrize("quantity", [Decimal("1e999999"), Decimal("0.0001")])
async def test_unusable_quantities_are_clarified_not_crashed(
    kind: ReportKind, quantity: Decimal
) -> None:
    clock = Clock(NOW)
    inbox = FakeInboxProvider(webhook_secret=WEBHOOK_SECRET, now=clock)
    mailbox = await inbox.provision_inbox("demo-incident")
    entity_name = "Shelter 4" if kind is ReportKind.NEED else "Depot B"
    location_text = "near Gandhi Park" if kind is ReportKind.NEED else "on River Road"
    inbox.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id=f"msg-{kind.value}-bad-qty",
        sender="sender@example.org",
        subject="Relief report",
        text=f"{entity_name} reports an implausible quantity.",
        received_at=NOW,
    )
    parsed = ParsedEmail(
        context=ReportContext(
            kind=kind,
            entity_name=entity_name,
            entity_type=EntityType.SHELTER if kind is ReportKind.NEED else EntityType.DEPOT,
            location_text=location_text,
            headcount=40 if kind is ReportKind.NEED else None,
            confidence=FieldConfidence(location=0.95, items=0.92, headcount=0.97),
        ),
        reports=[
            ParsedReport(
                resource=Resource.POTABLE_WATER,
                quantity=quantity,
                unit="l",
            )
        ],
    )
    pipeline = Pipeline(
        incident_id=INCIDENT_ID,
        inbox=inbox,
        intelligence=FakeIntelligenceProvider({entity_name: parsed}),
        geocoder=FakeGeocoder(
            {
                "gandhi park": (Decimal("18.520400"), Decimal("73.856700")),
                "river road": (Decimal("18.531000"), Decimal("73.871000")),
            }
        ),
        clock=clock,
    )

    result = await pipeline.process(
        await inbox.fetch_message(f"msg-{kind.value}-bad-qty")
    )

    assert result.clarification_question is not None
    assert result.kind is kind
    assert result.public_projection["needs"] == []
    assert result.public_projection["supplies"] == []
    assert len(inbox.sent_messages) == 1

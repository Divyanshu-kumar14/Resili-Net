"""Contract tests for sponsor adapter boundaries.

These tests never touch the network. The live Agentboxd adapter is exercised
through ``httpx.MockTransport`` so its request shape and error mapping are
verified without credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from resilinet.adapters.agentboxd import (
    AgentboxdInboxProvider,
    client_id_for_incident,
    sign_webhook,
)
from resilinet.adapters.fakes import FakeInboxProvider
from resilinet.ports import (
    LeaseExpiredError,
    MessageNotFoundError,
    OutboundDraft,
    ProviderUnavailableError,
    StaleWebhookError,
    WebhookPayloadError,
    WebhookVerificationError,
)
from resilinet.schemas import (
    FieldConfidence,
    ParsedEmail,
    ParsedReport,
    ReportContext,
    ReportKind,
    Resource,
    Urgency,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "agentboxd"
WEBHOOK_SECRET = "test-webhook-secret"
NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def signed_body(
    payload: Mapping[str, Any],
    *,
    timestamp: int | None = None,
    secret: str = WEBHOOK_SECRET,
) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    ts = timestamp if timestamp is not None else int(NOW.timestamp())
    headers = {
        "X-Mailroom-Timestamp": str(ts),
        "X-Mailroom-Signature": sign_webhook(raw, secret, ts),
    }
    return raw, headers


def live_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    clock: Clock | None = None,
) -> AgentboxdInboxProvider:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.agentboxd.test",
    )
    return AgentboxdInboxProvider(
        api_key="test-key",
        webhook_secret=WEBHOOK_SECRET,
        base_url="https://api.agentboxd.test",
        client=client,
        now=clock or Clock(NOW),
    )


def fake_provider(clock: Clock | None = None) -> FakeInboxProvider:
    return FakeInboxProvider(webhook_secret=WEBHOOK_SECRET, now=clock or Clock(NOW))


@pytest.fixture(params=["fake", "live"])
def inbox_provider(request: pytest.FixtureRequest) -> Any:
    if request.param == "fake":
        provider = fake_provider()
        withheld = load_fixture("message_withheld_full.json")
        provider.deliver_inbound(
            mailbox_id="inbox_02",
            provider_message_id=withheld["id"],
            sender=withheld["from"],
            subject=withheld["subject"],
            text=withheld["text"],
            received_at=datetime(2026, 11, 21, 12, 0, 1, tzinfo=UTC),
            withheld=True,
        )
        return provider

    withheld = load_fixture("message_withheld_full.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/messages/msg_02":
            return httpx.Response(200, json=withheld)
        return httpx.Response(500, json={"error": {"code": "unexpected"}})

    return live_provider(handler)


# ---------------------------------------------------------------------------
# Signature verification is the same for Fake and live parsing paths.
# ---------------------------------------------------------------------------


def test_sign_webhook_matches_the_documented_formula() -> None:
    raw = b'{"hello":"world"}'
    timestamp = 1_700_000_000
    expected = hmac.new(
        b"top-secret",
        f"{timestamp}.".encode() + raw,
        hashlib.sha256,
    ).hexdigest()

    assert sign_webhook(raw, "top-secret", timestamp) == expected


async def test_parse_webhook_accepts_a_valid_signature(inbox_provider: Any) -> None:
    payload = load_fixture("webhook_received_full.json")
    raw, headers = signed_body(payload)

    event = await inbox_provider.parse_webhook(raw, headers)

    assert event.event_type == "message.received"
    assert event.event_id == "evt_01"
    assert event.mailbox_id == "inbox_01"
    assert event.mailbox_address == "incident-a@homingbox.net"
    assert event.provider_message_id == "msg_01"
    assert event.thread_id == "thread_01"
    assert event.sender == "volunteer@example.org"
    assert event.subject == "Shelter 4 water"
    assert event.withheld is False
    assert event.raw is not None
    assert "40 people" in event.raw.text
    assert event.raw.attachment_count == 0


async def test_parse_webhook_rejects_a_tampered_body(inbox_provider: Any) -> None:
    payload = load_fixture("webhook_received_full.json")
    raw, headers = signed_body(payload)
    tampered = raw.replace(b"Shelter 4", b"Evil 9")

    with pytest.raises(WebhookVerificationError):
        await inbox_provider.parse_webhook(tampered, headers)


async def test_parse_webhook_rejects_a_stale_timestamp(inbox_provider: Any) -> None:
    payload = load_fixture("webhook_received_full.json")
    raw, headers = signed_body(payload, timestamp=int(NOW.timestamp()) - 301)

    with pytest.raises(StaleWebhookError):
        await inbox_provider.parse_webhook(raw, headers)


async def test_parse_webhook_rejects_a_future_timestamp(inbox_provider: Any) -> None:
    payload = load_fixture("webhook_received_full.json")
    raw, headers = signed_body(payload, timestamp=int(NOW.timestamp()) + 301)

    with pytest.raises(StaleWebhookError):
        await inbox_provider.parse_webhook(raw, headers)


async def test_parse_webhook_rejects_missing_signature_headers(inbox_provider: Any) -> None:
    payload = load_fixture("webhook_received_full.json")
    raw = json.dumps(payload, separators=(",", ":")).encode()

    with pytest.raises(WebhookVerificationError):
        await inbox_provider.parse_webhook(raw, {})


async def test_parse_webhook_verifies_before_json_parsing(inbox_provider: Any) -> None:
    payload = load_fixture("webhook_received_full.json")
    raw, headers = signed_body(payload)

    with pytest.raises(WebhookVerificationError):
        await inbox_provider.parse_webhook(raw[:-2], headers)

    malformed_raw = b"{not json"
    ts = int(NOW.timestamp())
    malformed_headers = {
        "X-Mailroom-Timestamp": str(ts),
        "X-Mailroom-Signature": sign_webhook(malformed_raw, WEBHOOK_SECRET, ts),
    }
    with pytest.raises(WebhookPayloadError):
        await inbox_provider.parse_webhook(malformed_raw, malformed_headers)


async def test_duplicate_received_enriched_and_released_events_share_message_identity(
    inbox_provider: Any,
) -> None:
    events = []
    for name in (
        "webhook_received_full.json",
        "webhook_enriched.json",
        "webhook_released.json",
    ):
        raw, headers = signed_body(load_fixture(name))
        events.append(await inbox_provider.parse_webhook(raw, headers))

    assert {event.provider_message_id for event in events} == {"msg_01"}
    assert [event.event_type for event in events] == [
        "message.received",
        "message.enriched",
        "message.released",
    ]
    assert len({event.event_id for event in events}) == 3
    assert all(event.raw is not None and "40 people" in event.raw.text for event in events)


async def test_withheld_envelope_requires_a_fetch(inbox_provider: Any) -> None:
    payload = load_fixture("webhook_withheld.json")
    raw, headers = signed_body(payload)

    event = await inbox_provider.parse_webhook(raw, headers)

    assert event.provider_message_id == "msg_02"
    assert event.withheld is True
    assert event.raw is None

    message = await inbox_provider.fetch_message(event.provider_message_id)

    assert message.provider_message_id == "msg_02"
    assert message.withheld is False
    assert "insulin" in message.text


async def test_envelope_payload_without_content_requires_a_fetch(inbox_provider: Any) -> None:
    payload = load_fixture("webhook_received_envelope.json")
    raw, headers = signed_body(payload)

    event = await inbox_provider.parse_webhook(raw, headers)

    assert event.provider_message_id == "msg_01"
    assert event.raw is None
    assert event.withheld is True


# ---------------------------------------------------------------------------
# Fake inbox behavior: persistent mailboxes, leases, idempotent sends.
# ---------------------------------------------------------------------------


async def test_provisioning_is_persistent_and_idempotent_for_the_same_incident() -> None:
    provider = fake_provider()

    first = await provider.provision_inbox("incident-a")
    second = await provider.provision_inbox("incident-a")
    other = await provider.provision_inbox("incident-b")

    assert first == second
    assert first.id != other.id
    assert first.address != other.address


async def test_claim_ack_and_nack_lease_lifecycle() -> None:
    clock = Clock(NOW)
    provider = fake_provider(clock)
    mailbox = await provider.provision_inbox("incident-a")
    provider.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-1",
        sender="a@example.org",
        subject="Water",
        text="We need water",
    )
    provider.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-2",
        sender="b@example.org",
        subject="Food",
        text="We need food",
    )

    claimed = await provider.claim(mailbox.id, lease_seconds=60, consumer="worker-1")
    assert [item.message.provider_message_id for item in claimed] == ["msg-1", "msg-2"]
    assert all(item.delivery_count == 1 for item in claimed)

    acked = await provider.ack("msg-1", claimed[0].lease_id)
    assert acked.message_id == "msg-1"
    assert acked.acked_at is not None

    nacked = await provider.nack("msg-2", claimed[1].lease_id, delay_seconds=30)
    assert nacked.delivery_count == 1
    assert nacked.dead_letter is False
    assert nacked.available_at == NOW + timedelta(seconds=30)

    assert await provider.claim(mailbox.id, lease_seconds=60, consumer="worker-1") == []

    clock.advance(seconds=31)
    reclaimed = await provider.claim(mailbox.id, lease_seconds=60, consumer="worker-1")
    assert [item.message.provider_message_id for item in reclaimed] == ["msg-2"]
    assert reclaimed[0].delivery_count == 2


async def test_expired_lease_can_be_reclaimed_and_stale_ack_is_rejected() -> None:
    clock = Clock(NOW)
    provider = fake_provider(clock)
    mailbox = await provider.provision_inbox("incident-a")
    provider.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-1",
        sender="a@example.org",
        subject="Water",
        text="We need water",
    )

    first = (await provider.claim(mailbox.id, lease_seconds=30, consumer="worker-1"))[0]
    clock.advance(seconds=31)
    second = (await provider.claim(mailbox.id, lease_seconds=30, consumer="worker-2"))[0]

    assert second.lease_id != first.lease_id
    assert second.delivery_count == 2

    with pytest.raises(LeaseExpiredError):
        await provider.ack("msg-1", first.lease_id)

    awaited = await provider.ack("msg-1", second.lease_id)
    assert awaited.message_id == "msg-1"
    assert await provider.claim(mailbox.id, 30, "worker-3") == []


async def test_repeated_nacks_dead_letter_the_message() -> None:
    provider = fake_provider()
    mailbox = await provider.provision_inbox("incident-a")
    provider.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-1",
        sender="a@example.org",
        subject="Water",
        text="We need water",
    )

    receipt: Any = None
    for attempt in range(1, 11):
        claimed = await provider.claim(mailbox.id, lease_seconds=5, consumer="worker-1")
        assert len(claimed) == 1, f"message should be claimable on attempt {attempt}"
        receipt = await provider.nack("msg-1", claimed[0].lease_id, delay_seconds=0)

    assert receipt.dead_letter is True
    assert await provider.claim(mailbox.id, lease_seconds=5, consumer="worker-1") == []
    assert (await provider.fetch_message("msg-1")).provider_message_id == "msg-1"


async def test_reply_routes_with_a_stable_idempotency_key() -> None:
    provider = fake_provider()
    mailbox = await provider.provision_inbox("incident-a")
    provider.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-1",
        sender="requester@example.org",
        subject="Water",
        text="We need water",
    )
    draft = OutboundDraft(
        mailbox_id=mailbox.id,
        to="requester@example.org",
        subject="Re: Water",
        text="Recommended: Depot B holds a reservation for you.",
        in_reply_to="msg-1",
    )

    first = await provider.reply("msg-1", draft, "outbox:row-1")
    second = await provider.reply("msg-1", draft, "outbox:row-1")

    assert first == second
    assert first.idempotency_key == "outbox:row-1"
    assert len(provider.sent_messages) == 1
    assert provider.sent_messages[0].draft.in_reply_to == "msg-1"


async def test_reply_refuses_an_arbitrary_in_reply_to_target() -> None:
    provider = fake_provider()
    mailbox = await provider.provision_inbox("incident-a")
    provider.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-1",
        sender="requester@example.org",
        subject="Water",
        text="We need water",
    )
    draft = OutboundDraft(
        mailbox_id=mailbox.id,
        to="requester@example.org",
        subject="Re: Water",
        text="reply",
        in_reply_to="msg-other",
    )

    with pytest.raises(ValueError):
        await provider.reply("msg-1", draft, "outbox:row-1")


async def test_send_is_idempotent_by_key() -> None:
    provider = fake_provider()
    mailbox = await provider.provision_inbox("incident-a")
    draft = OutboundDraft(
        mailbox_id=mailbox.id,
        to="depot@example.org",
        subject="Confirm stock",
        text="Please confirm the proposed reservation.",
    )

    first = await provider.send(draft, "outbox:row-2")
    second = await provider.send(draft, "outbox:row-2")

    assert first == second
    assert len(provider.sent_messages) == 1


async def test_fake_send_fault_injection_is_transient() -> None:
    provider = fake_provider()
    mailbox = await provider.provision_inbox("incident-a")
    provider.fail_next_send = ProviderUnavailableError("provider timed out")
    draft = OutboundDraft(
        mailbox_id=mailbox.id,
        to="depot@example.org",
        subject="Confirm stock",
        text="Please confirm.",
    )

    with pytest.raises(ProviderUnavailableError):
        await provider.send(draft, "outbox:row-3")

    receipt = await provider.send(draft, "outbox:row-3")
    assert receipt.provider_message_id


# ---------------------------------------------------------------------------
# Live adapter request shape, verified through MockTransport.
# ---------------------------------------------------------------------------


async def test_live_adapter_provisions_with_a_stable_client_id() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/v1/inboxes"
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["client_id"] == client_id_for_incident("incident-a")
        return httpx.Response(
            200,
            json={"id": "inbox_live", "address": "incident-a@homingbox.net"},
        )

    provider = live_provider(handler)

    first = await provider.provision_inbox("incident-a")
    second = await provider.provision_inbox("incident-a")

    assert first == second
    assert first.id == "inbox_live"
    assert len(requests) == 2


async def test_live_adapter_maps_claim_leases() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/inboxes/inbox_live/messages/claim"
        assert json.loads(request.content) == {
            "limit": 10,
            "lease_seconds": 45,
            "consumer": "worker-1",
        }
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "lease_id": "lease-1",
                        "lease_until": "2026-11-21T12:00:45.000Z",
                        "delivery_count": 2,
                        "message": load_fixture("webhook_received_full.json")["data"]["message"],
                    }
                ],
                "paused": False,
            },
        )

    provider = live_provider(handler)

    claimed = await provider.claim("inbox_live", lease_seconds=45, consumer="worker-1")

    assert len(claimed) == 1
    assert claimed[0].lease_id == "lease-1"
    assert claimed[0].delivery_count == 2
    assert claimed[0].lease_until == datetime(2026, 11, 21, 12, 0, 45, tzinfo=UTC)
    assert claimed[0].message.provider_message_id == "msg_01"


async def test_live_adapter_maps_ack_and_nack_responses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/messages/msg_01/ack":
            assert json.loads(request.content) == {"lease_id": "lease-1"}
            return httpx.Response(
                200, json={"id": "msg_01", "acked_at": "2026-11-21T12:01:00.000Z"}
            )
        if request.url.path == "/v1/messages/msg_01/nack":
            assert json.loads(request.content) == {"lease_id": "lease-2", "delay_seconds": 30}
            return httpx.Response(
                200,
                json={
                    "id": "msg_01",
                    "delivery_count": 3,
                    "available_at": "2026-11-21T12:01:30.000Z",
                    "dead_letter": True,
                },
            )
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    provider = live_provider(handler)

    acked = await provider.ack("msg_01", "lease-1")
    assert acked.acked_at == datetime(2026, 11, 21, 12, 1, 0, tzinfo=UTC)
    assert acked.gone is False

    nacked = await provider.nack("msg_01", "lease-2", delay_seconds=30)
    assert nacked.delivery_count == 3
    assert nacked.dead_letter is True
    assert nacked.available_at == datetime(2026, 11, 21, 12, 1, 30, tzinfo=UTC)


async def test_live_adapter_maps_expired_leases_and_missing_messages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ack"):
            return httpx.Response(
                409,
                json={"error": {"code": "lease_expired", "message": "lease expired"}},
            )
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "gone"}})

    provider = live_provider(handler)

    with pytest.raises(LeaseExpiredError):
        await provider.ack("msg_01", "lease-old")
    with pytest.raises(MessageNotFoundError):
        await provider.fetch_message("msg_missing")


async def test_live_adapter_maps_provider_outages_as_retryable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"code": "unavailable"}})

    provider = live_provider(handler)
    draft = OutboundDraft(
        mailbox_id="inbox_live",
        to="depot@example.org",
        subject="Confirm stock",
        text="Please confirm.",
    )

    with pytest.raises(ProviderUnavailableError) as excinfo:
        await provider.send(draft, "outbox:row-4")

    assert excinfo.value.retryable is True


async def test_live_adapter_replies_without_an_in_reply_to_argument() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/inboxes/inbox_live/messages/msg_01/reply"
        assert request.headers["idempotency-key"] == "outbox:row-5"
        body = json.loads(request.content)
        assert body == {"text": "Recommended: Depot B."}
        assert "in_reply_to" not in body
        return httpx.Response(
            202,
            json={"message": {"id": "msg_out_1", "status": "queued"}},
        )

    provider = live_provider(handler)
    draft = OutboundDraft(
        mailbox_id="inbox_live",
        to="requester@example.org",
        subject="Re: Water",
        text="Recommended: Depot B.",
        in_reply_to="msg_01",
    )

    receipt = await provider.reply("msg_01", draft, "outbox:row-5")

    assert receipt.provider_message_id == "msg_out_1"
    assert receipt.status == "queued"


async def test_live_adapter_sends_with_an_idempotency_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/inboxes/inbox_live/messages/send"
        assert request.headers["idempotency-key"] == "outbox:row-6"
        assert json.loads(request.content) == {
            "to": "depot@example.org",
            "subject": "Confirm stock",
            "text": "Please confirm.",
        }
        return httpx.Response(202, json={"message": {"id": "msg_out_2", "status": "queued"}})

    provider = live_provider(handler)
    draft = OutboundDraft(
        mailbox_id="inbox_live",
        to="depot@example.org",
        subject="Confirm stock",
        text="Please confirm.",
    )

    receipt = await provider.send(draft, "outbox:row-6")

    assert receipt.provider_message_id == "msg_out_2"


# ---------------------------------------------------------------------------
# Domain schema boundaries used by the adapter protocols.
# ---------------------------------------------------------------------------


def test_parsed_email_rejects_invalid_confidence_and_quantities() -> None:
    with pytest.raises(ValidationError):
        FieldConfidence(location=1.1, items=0.9, headcount=0.9)

    with pytest.raises(ValidationError):
        ReportContext(
            kind=ReportKind.NEED,
            entity_name="Shelter 4",
            headcount=-1,
            confidence=FieldConfidence(location=0.9, items=0.9, headcount=0.9),
        )

    with pytest.raises(ValidationError):
        ParsedReport(resource=Resource.POTABLE_WATER, quantity=Decimal("-1"))

    with pytest.raises(ValueError):
        Resource("water")


def test_parsed_email_round_trips_a_need_report() -> None:
    parsed = ParsedEmail(
        context=ReportContext(
            kind=ReportKind.NEED,
            entity_name="Shelter 4",
            entity_type="shelter",
            location_text="near Gandhi Park",
            headcount=40,
            urgency=Urgency.NORMAL,
            confidence=FieldConfidence(location=0.9, items=0.9, headcount=0.95),
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

    assert parsed.reports[0].resource is Resource.POTABLE_WATER
    assert parsed.context.headcount == 40

"""Edge cases the happy-path contract suite did not cover."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from resilinet.adapters.agentboxd import AgentboxdInboxProvider, client_id_for_incident
from resilinet.adapters.fakes import FakeInboxProvider
from resilinet.ports import (
    LeaseExpiredError,
    ProviderUnavailableError,
    WebhookVerificationError,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "agentboxd"
WEBHOOK_SECRET = "test-webhook-secret"
NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def signed_body(payload: Mapping[str, Any]) -> tuple[bytes, dict[str, str]]:
    from resilinet.adapters.agentboxd import sign_webhook

    raw = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = int(NOW.timestamp())
    return raw, {
        "X-Mailroom-Timestamp": str(timestamp),
        "X-Mailroom-Signature": sign_webhook(raw, WEBHOOK_SECRET, timestamp),
    }


def live_provider(handler: Any) -> AgentboxdInboxProvider:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.agentboxd.test",
    )
    return AgentboxdInboxProvider(
        api_key="test-key",
        webhook_secret=WEBHOOK_SECRET,
        base_url="https://api.agentboxd.test",
        client=client,
        now=lambda: NOW,
    )


def fake_provider() -> FakeInboxProvider:
    return FakeInboxProvider(webhook_secret=WEBHOOK_SECRET, now=lambda: NOW)


def test_client_id_is_collision_resistant() -> None:
    first = client_id_for_incident("incident/a")
    second = client_id_for_incident("incident a")
    third = client_id_for_incident("INC-A")

    assert len({first, second, third}) == 3
    assert all(value.startswith("resilinet-") for value in (first, second, third))
    assert all(len(value) <= 120 for value in (first, second, third))


async def test_fake_provisioning_distinguishes_similar_incident_keys() -> None:
    provider = fake_provider()

    first = await provider.provision_inbox("incident/a")
    second = await provider.provision_inbox("incident a")

    assert first.id != second.id
    assert first.address != second.address


async def test_live_provisioning_distinguishes_similar_incident_keys() -> None:
    client_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        client_ids.append(body["client_id"])
        return httpx.Response(
            200,
            json={"id": f"inbox_{len(client_ids)}", "address": f"box{len(client_ids)}@example.net"},
        )

    provider = live_provider(handler)

    first = await provider.provision_inbox("incident/a")
    second = await provider.provision_inbox("incident a")

    assert first.id != second.id
    assert len(set(client_ids)) == 2


@pytest.mark.parametrize("kind", ["fake", "live"])
async def test_parse_rejects_a_non_ascii_signature(kind: str) -> None:
    provider = fake_provider() if kind == "fake" else live_provider(
        lambda _: httpx.Response(500)
    )
    payload = load_fixture("webhook_received_full.json")
    raw, headers = signed_body(payload)
    headers["X-Mailroom-Signature"] = "ünicode"

    with pytest.raises(WebhookVerificationError):
        await provider.parse_webhook(raw, headers)


@pytest.mark.parametrize("kind", ["fake", "live"])
async def test_parse_delivers_an_empty_body_without_marking_it_withheld(kind: str) -> None:
    provider = fake_provider() if kind == "fake" else live_provider(
        lambda _: httpx.Response(500)
    )
    payload = {
        "id": "evt_empty",
        "type": "message.received",
        "created_at": "2026-11-21T12:00:00.000Z",
        "data": {
            "inbox": {"id": "inbox_01", "address": "incident-a@homingbox.net"},
            "thread_id": "thread_empty",
            "message": {
                "id": "msg_empty",
                "from": "sender@example.org",
                "subject": "Attachment only",
                "text": "",
                "received_at": "2026-11-21T11:59:58.000Z",
                "withheld": False,
            },
        },
    }
    raw, headers = signed_body(payload)

    event = await provider.parse_webhook(raw, headers)

    assert event.withheld is False
    assert event.raw is not None
    assert event.raw.text == ""


@pytest.mark.parametrize("kind", ["fake", "live"])
async def test_envelope_empty_body_is_not_withheld(kind: str) -> None:
    provider = fake_provider() if kind == "fake" else live_provider(
        lambda _: httpx.Response(500)
    )
    payload = {
        "id": "evt_envelope_empty",
        "type": "message.received",
        "created_at": "2026-11-21T12:00:00.000Z",
        "data": {
            "inbox_id": "inbox_01",
            "thread_id": "thread_empty",
            "message_id": "msg_envelope_empty",
            "from": "sender@example.org",
            "to": "incident-a@homingbox.net",
            "subject": "Attachment only",
            "received_at": "2026-11-21T11:59:58.000Z",
            "withheld": False,
            "text": "",
        },
    }
    raw, headers = signed_body(payload)

    event = await provider.parse_webhook(raw, headers)

    assert event.withheld is False
    assert event.raw is not None
    assert event.raw.text == ""


async def test_live_maps_string_shaped_error_codes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": "lease_expired"})

    provider = live_provider(handler)

    with pytest.raises(LeaseExpiredError):
        await provider.ack("msg_01", "lease-old")


async def test_live_maps_rate_limits_and_empty_claims() -> None:
    def rate_limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"code": "rate_limited"}})

    provider = live_provider(rate_limited)
    with pytest.raises(ProviderUnavailableError) as excinfo:
        await provider.claim("inbox_01", 30, "worker-1")
    assert excinfo.value.retryable is True

    empty = live_provider(lambda _: httpx.Response(200, json={"data": [], "paused": False}))
    assert await empty.claim("inbox_01", 30, "worker-1") == []


async def test_live_rejects_malformed_claim_items() -> None:
    provider = live_provider(
        lambda _: httpx.Response(200, json={"data": [{"lease_id": "l"}], "paused": False})
    )

    with pytest.raises(ProviderUnavailableError):
        await provider.claim("inbox_01", 30, "worker-1")


async def test_live_maps_a_gone_ack() -> None:
    provider = live_provider(
        lambda _: httpx.Response(200, json={"id": "msg_01", "gone": True})
    )

    receipt = await provider.ack("msg_01", "lease-1")

    assert receipt.gone is True
    assert receipt.acked_at is None


async def test_fake_fetch_ack_and_nack_fault_injection() -> None:
    provider = fake_provider()
    mailbox = await provider.provision_inbox("incident-a")
    provider.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-1",
        sender="sender@example.org",
        subject="Water",
        text="We need water",
    )

    provider.fail_next_fetch = ProviderUnavailableError("fetch failed")
    with pytest.raises(ProviderUnavailableError):
        await provider.fetch_message("msg-1")

    claimed = await provider.claim(mailbox.id, lease_seconds=60, consumer="worker-1")
    provider.fail_next_ack = ProviderUnavailableError("ack failed")
    with pytest.raises(ProviderUnavailableError):
        await provider.ack("msg-1", claimed[0].lease_id)

    provider.fail_next_nack = ProviderUnavailableError("nack failed")
    with pytest.raises(ProviderUnavailableError):
        await provider.nack("msg-1", claimed[0].lease_id, delay_seconds=0)

    assert (await provider.nack("msg-1", claimed[0].lease_id, delay_seconds=0)).message_id == "msg-1"


async def test_fake_ack_is_idempotent_but_ack_after_nack_is_stale() -> None:
    provider = fake_provider()
    mailbox = await provider.provision_inbox("incident-a")
    provider.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-1",
        sender="sender@example.org",
        subject="Water",
        text="We need water",
    )
    claimed = (await provider.claim(mailbox.id, 60, "worker-1"))[0]

    first = await provider.ack("msg-1", claimed.lease_id)
    second = await provider.ack("msg-1", "different-lease")
    assert first.acked_at == second.acked_at

    provider.deliver_inbound(
        mailbox_id=mailbox.id,
        provider_message_id="msg-2",
        sender="sender@example.org",
        subject="Food",
        text="We need food",
    )
    claimed_2 = (await provider.claim(mailbox.id, 60, "worker-1"))[0]
    await provider.nack("msg-2", claimed_2.lease_id, delay_seconds=0)
    with pytest.raises(LeaseExpiredError):
        await provider.ack("msg-2", claimed_2.lease_id)

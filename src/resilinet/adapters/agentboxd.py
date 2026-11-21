"""Agentboxd persistent-inbox adapter.

Documented contract, verified against the public API reference on 2026-11-21;
an account-level live spike is still pending (see docs/evidence/phase0.md):

- webhook headers ``X-Mailroom-Timestamp`` and ``X-Mailroom-Signature`` where
  the signature is ``hex(HMAC-SHA256(secret, "<timestamp>.<raw body>"))`` and
  timestamps more than 300 seconds off are rejected;
- envelope payloads carry no body, so a withheld or envelope event must be
  fetched with ``GET /v1/messages/:id``;
- replies use ``POST /v1/inboxes/:id/messages/:messageId/reply`` and the
  provider owns ``In-Reply-To``/``References``;
- sends and replies honour ``Idempotency-Key``;
- the claim/ack queue leases inbound messages and answers ``409 lease_expired``
  when a lease was replaced.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from ..ports import (
    AckReceipt,
    AdapterError,
    ClaimedMessage,
    InboundEvent,
    LeaseExpiredError,
    Mailbox,
    MessageNotFoundError,
    NackReceipt,
    OutboundDraft,
    ProviderUnavailableError,
    RawMessage,
    SentReceipt,
    StaleWebhookError,
    WebhookPayloadError,
    WebhookVerificationError,
)

logger = logging.getLogger("resilinet.adapters.agentboxd")

MESSAGE_EVENTS = frozenset({"message.received", "message.enriched", "message.released"})
MAX_CLOCK_SKEW_SECONDS = 300
CLAIM_LIMIT = 10


def sign_webhook(raw: bytes, secret: str, timestamp: int) -> str:
    """Return the documented lowercase HMAC-SHA256 signature for a raw body."""
    message = f"{timestamp}.".encode("ascii") + raw
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def client_id_for_incident(incident_key: str) -> str:
    """Stable, collision-resistant idempotency key for a persistent inbox.

    The raw incident key is hashed rather than slugified: ``incident/a`` and
    ``incident a`` must never share an inbox. The provider treats ``client_id``
    as the idempotency key for ``POST /v1/inboxes``.
    """
    digest = hashlib.sha256(incident_key.encode("utf-8")).hexdigest()[:40]
    return f"resilinet-{digest}"


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _parse_datetime(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        raise ValueError(f"missing or invalid {field_name}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    return _parse_datetime(value, "timestamp")


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sender_address(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("address", "email", "from"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return ""


def _message_from_payload(
    message: Mapping[str, Any],
    *,
    mailbox_id: str | None = None,
) -> RawMessage:
    message_id = str(message.get("id") or message.get("message_id") or "").strip()
    if not message_id:
        raise ValueError("message payload has no id")
    sender = _sender_address(message.get("from"))
    if not sender:
        raise ValueError("message payload has no sender")
    withheld = bool(message.get("withheld", False))
    text = "" if withheld else str(message.get("text") or message.get("extracted_text") or "")
    headers = _as_mapping(message.get("headers"))
    attachments = message.get("attachments")
    attachment_count = len(attachments) if isinstance(attachments, list) else 0
    return RawMessage(
        provider_message_id=message_id,
        mailbox_id=str(mailbox_id or message.get("inbox_id") or ""),
        sender=sender,
        subject=str(message.get("subject") or ""),
        text=text,
        received_at=_parse_datetime(message.get("received_at"), "received_at"),
        thread_id=(
            str(message["thread_id"]) if message.get("thread_id") is not None else None
        ),
        attachment_count=attachment_count,
        withheld=withheld,
        headers={str(key): str(value) for key, value in headers.items()},
    )


def parse_signed_webhook(
    raw: bytes,
    headers: Mapping[str, str],
    *,
    webhook_secret: str,
    now: Callable[[], datetime],
) -> InboundEvent:
    """Verify and parse one signed Agentboxd message event.

    Signature verification happens on the raw bytes before JSON parsing.
    """
    timestamp_header = _header(headers, "X-Mailroom-Timestamp")
    signature_header = _header(headers, "X-Mailroom-Signature")
    if not timestamp_header or not signature_header:
        raise WebhookVerificationError("missing signature headers")

    try:
        timestamp = int(timestamp_header)
    except ValueError as exc:
        raise WebhookVerificationError("invalid timestamp header") from exc

    if abs(int(now().timestamp()) - timestamp) > MAX_CLOCK_SKEW_SECONDS:
        raise StaleWebhookError("envelope timestamp outside the 300 second window")

    expected = sign_webhook(raw, webhook_secret, timestamp)
    provided = signature_header.strip().lower().encode("utf-8", "replace")
    if not hmac.compare_digest(expected.encode("ascii"), provided):
        raise WebhookVerificationError("signature mismatch")

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WebhookPayloadError("verified envelope is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise WebhookPayloadError("verified envelope is not a JSON object")

    event_type = str(payload.get("type") or "")
    if event_type not in MESSAGE_EVENTS:
        raise WebhookPayloadError(f"unsupported event type: {event_type or 'missing'}")

    data = _as_mapping(payload.get("data"))
    created_at = payload.get("created_at")
    event_id = str(payload.get("id") or "")

    message = data.get("message")
    if isinstance(message, Mapping):
        try:
            raw_message = _message_from_payload(message)
        except ValueError as exc:
            raise WebhookPayloadError(str(exc)) from exc
        inbox = _as_mapping(data.get("inbox"))
        mailbox_id = str(inbox.get("id") or raw_message.mailbox_id)
        mailbox_address = inbox.get("address")
        content_available = not raw_message.withheld
        return InboundEvent(
            event_id=event_id,
            event_type=event_type,
            mailbox_id=mailbox_id,
            mailbox_address=str(mailbox_address) if mailbox_address else None,
            provider_message_id=raw_message.provider_message_id,
            thread_id=raw_message.thread_id,
            sender=raw_message.sender,
            subject=raw_message.subject,
            received_at=raw_message.received_at,
            withheld=not content_available,
            raw=raw_message if content_available else None,
        )

    provider_message_id = str(data.get("message_id") or "").strip()
    if not provider_message_id:
        raise WebhookPayloadError("envelope has no message id")
    sender = _sender_address(data.get("from"))
    if not sender:
        raise WebhookPayloadError("envelope has no sender")
    text_present = "text" in data
    text = str(data.get("text") or "")
    withheld = bool(data.get("withheld", False)) or not text_present
    try:
        received_at = _parse_datetime(
            data.get("received_at") or created_at, "received_at"
        )
    except ValueError as exc:
        raise WebhookPayloadError(str(exc)) from exc
    mailbox_id = str(data.get("inbox_id") or "")
    mailbox_address = data.get("inbox_address")
    thread_id = data.get("thread_id")
    subject = str(data.get("subject") or "")
    normalized_thread_id = str(thread_id) if thread_id is not None else None
    envelope_raw: RawMessage | None = None
    if not withheld:
        envelope_raw = RawMessage(
            provider_message_id=provider_message_id,
            mailbox_id=mailbox_id,
            sender=sender,
            subject=subject,
            text=text,
            received_at=received_at,
            thread_id=normalized_thread_id,
            withheld=False,
        )
    return InboundEvent(
        event_id=event_id,
        event_type=event_type,
        mailbox_id=mailbox_id,
        mailbox_address=str(mailbox_address) if mailbox_address else None,
        provider_message_id=provider_message_id,
        thread_id=normalized_thread_id,
        sender=sender,
        subject=subject,
        received_at=received_at,
        withheld=withheld,
        raw=envelope_raw,
    )


class AgentboxdInboxProvider:
    """Live Agentboxd implementation of :class:`~resilinet.ports.InboxProvider`."""

    def __init__(
        self,
        *,
        api_key: str,
        webhook_secret: str,
        base_url: str = "https://api.agentboxd.com",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 20.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not api_key or not webhook_secret:
            raise ValueError("Agentboxd api_key and webhook_secret are required")
        self._api_key = api_key
        self._webhook_secret = webhook_secret
        self._base_url = base_url.rstrip("/")
        self._now = now or (lambda: datetime.now(UTC))
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def provision_inbox(self, incident_key: str) -> Mailbox:
        payload = await self._request(
            "POST",
            "/v1/inboxes",
            json_body={"client_id": client_id_for_incident(incident_key)},
        )
        return Mailbox(
            id=_required_text(payload, "id"),
            address=_required_text(payload, "address"),
        )

    async def parse_webhook(
        self, raw: bytes, headers: Mapping[str, str]
    ) -> InboundEvent:
        return parse_signed_webhook(
            raw,
            headers,
            webhook_secret=self._webhook_secret,
            now=self._now,
        )

    async def fetch_message(self, provider_message_id: str) -> RawMessage:
        payload = await self._request(
            "GET", f"/v1/messages/{quote(provider_message_id, safe='')}"
        )
        try:
            return _message_from_payload(payload)
        except ValueError as exc:
            raise ProviderUnavailableError(
                "malformed Agentboxd message response", retryable=True
            ) from exc

    async def claim(
        self, mailbox_id: str, lease_seconds: int, consumer: str
    ) -> list[ClaimedMessage]:
        payload = await self._request(
            "POST",
            f"/v1/inboxes/{quote(mailbox_id, safe='')}/messages/claim",
            json_body={
                "limit": CLAIM_LIMIT,
                "lease_seconds": lease_seconds,
                "consumer": consumer,
            },
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise ProviderUnavailableError(
                "malformed Agentboxd claim response", retryable=True
            )
        claimed: list[ClaimedMessage] = []
        for item in data:
            if not isinstance(item, Mapping):
                raise ProviderUnavailableError(
                    "malformed Agentboxd claim item", retryable=True
                )
            try:
                message_payload = _as_mapping(item.get("message"))
                claimed.append(
                    ClaimedMessage(
                        lease_id=str(item["lease_id"]),
                        lease_until=_parse_datetime(item["lease_until"], "lease_until"),
                        delivery_count=int(item.get("delivery_count", 1)),
                        message=_message_from_payload(
                            message_payload, mailbox_id=mailbox_id
                        ),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderUnavailableError(
                    "malformed Agentboxd claim item", retryable=True
                ) from exc
        return claimed

    async def ack(self, provider_message_id: str, lease_id: str) -> AckReceipt:
        payload = await self._request(
            "POST",
            f"/v1/messages/{quote(provider_message_id, safe='')}/ack",
            json_body={"lease_id": lease_id},
        )
        try:
            return AckReceipt(
                message_id=str(payload.get("id") or provider_message_id),
                acked_at=_optional_datetime(payload.get("acked_at")),
                gone=bool(payload.get("gone", False)),
            )
        except ValueError as exc:
            raise ProviderUnavailableError(
                "malformed Agentboxd ack response", retryable=True
            ) from exc

    async def nack(
        self, provider_message_id: str, lease_id: str, delay_seconds: int
    ) -> NackReceipt:
        payload = await self._request(
            "POST",
            f"/v1/messages/{quote(provider_message_id, safe='')}/nack",
            json_body={"lease_id": lease_id, "delay_seconds": delay_seconds},
        )
        try:
            return NackReceipt(
                message_id=str(payload.get("id") or provider_message_id),
                delivery_count=int(payload.get("delivery_count", 0)),
                available_at=_parse_datetime(payload.get("available_at"), "available_at"),
                dead_letter=bool(payload.get("dead_letter", False)),
            )
        except (TypeError, ValueError) as exc:
            raise ProviderUnavailableError(
                "malformed Agentboxd nack response", retryable=True
            ) from exc

    async def send(self, draft: OutboundDraft, idempotency_key: str) -> SentReceipt:
        if draft.in_reply_to is not None:
            raise ValueError("send() cannot set in_reply_to; use reply()")
        payload = await self._request(
            "POST",
            f"/v1/inboxes/{quote(draft.mailbox_id, safe='')}/messages/send",
            json_body={
                "to": draft.to,
                "subject": draft.subject,
                "text": draft.text,
            },
            idempotency_key=idempotency_key,
        )
        return _sent_receipt(payload, idempotency_key)

    async def reply(
        self,
        provider_message_id: str,
        draft: OutboundDraft,
        idempotency_key: str,
    ) -> SentReceipt:
        if draft.in_reply_to is not None and draft.in_reply_to != provider_message_id:
            raise ValueError("reply() target does not match in_reply_to")
        payload = await self._request(
            "POST",
            (
                f"/v1/inboxes/{quote(draft.mailbox_id, safe='')}"
                f"/messages/{quote(provider_message_id, safe='')}/reply"
            ),
            json_body={"text": draft.text},
            idempotency_key=idempotency_key,
        )
        return _sent_receipt(payload, idempotency_key)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Mapping[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        try:
            response = await self._client.request(
                method,
                path,
                json=json_body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                "Agentboxd request failed", retryable=True
            ) from exc

        if response.status_code >= 400:
            raise _provider_error(response)
        if response.status_code == 204 or not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderUnavailableError(
                "Agentboxd returned malformed JSON", retryable=True
            ) from exc
        if not isinstance(payload, Mapping):
            raise ProviderUnavailableError(
                "Agentboxd returned an unexpected payload", retryable=True
            )
        return payload


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProviderUnavailableError(
            f"Agentboxd response is missing {key}", retryable=True
        )
    return value.strip()


def _sent_receipt(payload: Mapping[str, Any], idempotency_key: str) -> SentReceipt:
    message = _as_mapping(payload.get("message"))
    source = message or payload
    message_id = str(source.get("id") or payload.get("sent_message_id") or "").strip()
    if not message_id:
        raise ProviderUnavailableError(
            "Agentboxd response is missing the sent message id", retryable=True
        )
    return SentReceipt(
        provider_message_id=message_id,
        status=str(source.get("status") or payload.get("status") or "queued"),
        idempotency_key=idempotency_key,
    )


def _provider_error(response: httpx.Response) -> AdapterError:
    code = ""
    try:
        payload = response.json()
        if isinstance(payload, Mapping):
            error = payload.get("error")
            if isinstance(error, Mapping):
                code = str(error.get("code") or "")
            elif isinstance(error, str):
                code = error
    except ValueError:
        code = ""

    if response.status_code == 409 and code == "lease_expired":
        return LeaseExpiredError("Agentboxd lease expired")
    if response.status_code == 404:
        return MessageNotFoundError("Agentboxd message not found")
    retryable = response.status_code == 429 or response.status_code >= 500
    return ProviderUnavailableError(
        f"Agentboxd returned {response.status_code}" + (f" ({code})" if code else ""),
        retryable=retryable,
    )

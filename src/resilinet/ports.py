"""Adapter Protocols and transport-neutral DTOs.

Provider payloads never leave an adapter: callers receive these types and the
same error family from Fake, replayed, and live implementations.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from .schemas import CriticContext, CriticVerdict, GeoConfidence, ParsedEmail


class AdapterError(Exception):
    """Base class for provider-independent adapter failures."""


class WebhookVerificationError(AdapterError):
    """Signature, timestamp, or required header verification failed."""


class StaleWebhookError(WebhookVerificationError):
    """A correctly formed envelope arrived outside the replay window."""


class WebhookPayloadError(AdapterError):
    """A verified envelope did not match the documented event shape."""


class MessageNotFoundError(AdapterError):
    """The provider no longer has the requested message."""


class LeaseExpiredError(AdapterError):
    """The lease presented by ack/nack is no longer the current lease."""


class ProviderUnavailableError(AdapterError):
    """A provider call failed; ``retryable`` guides worker scheduling."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class ExtractionError(AdapterError):
    """The intelligence provider could not produce a schema-valid report."""


@dataclass(frozen=True, slots=True)
class Mailbox:
    id: str
    address: str


@dataclass(frozen=True, slots=True)
class RawMessage:
    provider_message_id: str
    mailbox_id: str
    sender: str
    subject: str
    text: str
    received_at: datetime
    thread_id: str | None = None
    attachment_count: int = 0
    withheld: bool = False
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InboundEvent:
    event_id: str
    event_type: str
    mailbox_id: str
    provider_message_id: str
    sender: str
    subject: str
    received_at: datetime
    withheld: bool
    mailbox_address: str | None = None
    thread_id: str | None = None
    raw: RawMessage | None = None


@dataclass(frozen=True, slots=True)
class ClaimedMessage:
    lease_id: str
    lease_until: datetime
    delivery_count: int
    message: RawMessage


@dataclass(frozen=True, slots=True)
class AckReceipt:
    message_id: str
    acked_at: datetime | None
    gone: bool = False


@dataclass(frozen=True, slots=True)
class NackReceipt:
    message_id: str
    delivery_count: int
    available_at: datetime
    dead_letter: bool


@dataclass(frozen=True, slots=True)
class OutboundDraft:
    mailbox_id: str
    to: str
    subject: str
    text: str
    in_reply_to: str | None = None


@dataclass(frozen=True, slots=True)
class SentReceipt:
    provider_message_id: str
    status: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class VerificationRequest:
    state_id: str
    status: str


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    email: str
    verified: bool
    provider_subject: str | None = None


@dataclass(frozen=True, slots=True)
class GeoResult:
    lat: Decimal | None
    lon: Decimal | None
    confidence: GeoConfidence
    source: str


class InboxProvider(Protocol):
    async def provision_inbox(self, incident_key: str) -> Mailbox: ...

    async def parse_webhook(
        self, raw: bytes, headers: Mapping[str, str]
    ) -> InboundEvent: ...

    async def fetch_message(self, provider_message_id: str) -> RawMessage: ...

    async def claim(
        self, mailbox_id: str, lease_seconds: int, consumer: str
    ) -> list[ClaimedMessage]: ...

    async def ack(self, provider_message_id: str, lease_id: str) -> AckReceipt: ...

    async def nack(
        self, provider_message_id: str, lease_id: str, delay_seconds: int
    ) -> NackReceipt: ...

    async def send(self, draft: OutboundDraft, idempotency_key: str) -> SentReceipt: ...

    async def reply(
        self, provider_message_id: str, draft: OutboundDraft, idempotency_key: str
    ) -> SentReceipt: ...

    async def aclose(self) -> None: ...


class AuthProvider(Protocol):
    async def send_magic_link(self, email: str, redirect_url: str) -> VerificationRequest: ...

    async def verify_callback(self, params: Mapping[str, str]) -> VerifiedIdentity: ...


class IntelligenceProvider(Protocol):
    async def extract(self, message: RawMessage) -> ParsedEmail: ...

    async def critique(self, proposal: CriticContext) -> CriticVerdict: ...


class Geocoder(Protocol):
    async def geocode(self, incident_id: UUID, location_text: str) -> GeoResult: ...


__all__ = [
    "AckReceipt",
    "AdapterError",
    "AuthProvider",
    "ClaimedMessage",
    "ExtractionError",
    "GeoResult",
    "Geocoder",
    "InboundEvent",
    "InboxProvider",
    "IntelligenceProvider",
    "LeaseExpiredError",
    "Mailbox",
    "MessageNotFoundError",
    "NackReceipt",
    "OutboundDraft",
    "ProviderUnavailableError",
    "RawMessage",
    "SentReceipt",
    "StaleWebhookError",
    "VerificationRequest",
    "VerifiedIdentity",
    "WebhookPayloadError",
    "WebhookVerificationError",
]

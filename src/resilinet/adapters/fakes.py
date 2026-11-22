"""Deterministic in-memory adapters for tests, replay, and the demo."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from ..ports import (
    AckReceipt,
    AdapterError,
    ClaimedMessage,
    ExtractionError,
    GeoResult,
    InboundEvent,
    LeaseExpiredError,
    Mailbox,
    MessageNotFoundError,
    NackReceipt,
    OutboundDraft,
    ProviderUnavailableError,
    RawMessage,
    SentReceipt,
    VerificationRequest,
    VerifiedIdentity,
)
from ..schemas import CriticContext, CriticVerdict, GeoConfidence, ParsedEmail
from .agentboxd import client_id_for_incident, parse_signed_webhook


@dataclass
class _StoredMessage:
    raw: RawMessage
    full_text: str
    mailbox_id: str
    available_at: datetime
    acked_at: datetime | None = None
    dead_letter: bool = False
    delivery_count: int = 0
    lease_id: str | None = None
    lease_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class SentDelivery:
    draft: OutboundDraft
    idempotency_key: str
    receipt: SentReceipt


class FakeInboxProvider:
    """Offline implementation of the persistent inbox contract.

    It mirrors the Agentboxd semantics that ResiliNet depends on: idempotent
    inbox provisioning, at-least-once claims with expiring leases, dead letters
    after repeated nacks, and send/reply idempotency by key.
    """

    def __init__(
        self,
        *,
        webhook_secret: str = "fake-webhook-secret",
        now: Callable[[], datetime] | None = None,
        max_claims: int = 10,
        page_size: int = 10,
    ) -> None:
        self._webhook_secret = webhook_secret
        self._now = now or (lambda: datetime.now(UTC))
        self._max_claims = max_claims
        self._page_size = page_size
        self._mailboxes: dict[str, Mailbox] = {}
        self._incident_mailboxes: dict[str, str] = {}
        self._messages: dict[str, _StoredMessage] = {}
        self._deliveries: dict[str, SentDelivery] = {}
        self._lease_counter = 0
        self._send_counter = 0
        self.fail_next_send: AdapterError | None = None
        self.fail_next_claim: AdapterError | None = None
        self.fail_next_fetch: AdapterError | None = None
        self.fail_next_ack: AdapterError | None = None
        self.fail_next_nack: AdapterError | None = None

    @property
    def sent_messages(self) -> tuple[SentDelivery, ...]:
        return tuple(self._deliveries.values())

    async def provision_inbox(self, incident_key: str) -> Mailbox:
        key = client_id_for_incident(incident_key)
        existing = self._incident_mailboxes.get(key)
        if existing is not None:
            return self._mailboxes[existing]
        mailbox = Mailbox(
            id=f"fake-inbox-{len(self._mailboxes) + 1}",
            address=f"inbox-{len(self._mailboxes) + 1}@fake.homingbox.test",
        )
        self._mailboxes[mailbox.id] = mailbox
        self._incident_mailboxes[key] = mailbox.id
        return mailbox

    def deliver_inbound(
        self,
        *,
        mailbox_id: str,
        provider_message_id: str,
        sender: str,
        subject: str,
        text: str,
        thread_id: str | None = None,
        received_at: datetime | None = None,
        withheld: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> RawMessage:
        """Store an inbound message as the provider would before delivery."""
        received = received_at or self._now()
        raw = RawMessage(
            provider_message_id=provider_message_id,
            mailbox_id=mailbox_id,
            sender=sender,
            subject=subject,
            text="" if withheld else text,
            received_at=received,
            thread_id=thread_id,
            withheld=withheld,
            headers=dict(headers or {}),
        )
        self._messages[provider_message_id] = _StoredMessage(
            raw=raw,
            full_text=text,
            mailbox_id=mailbox_id,
            available_at=received,
        )
        return raw

    async def parse_webhook(
        self, raw: bytes, headers: Mapping[str, str]
    ) -> InboundEvent:
        event = parse_signed_webhook(
            raw,
            headers,
            webhook_secret=self._webhook_secret,
            now=self._now,
        )
        if event.mailbox_id and event.mailbox_id not in self._mailboxes:
            # A webhook refers to a mailbox the fake already knows in production.
            self._mailboxes[event.mailbox_id] = Mailbox(
                id=event.mailbox_id,
                address=event.mailbox_address
                or f"{event.mailbox_id}@fake.homingbox.test",
            )
        if event.raw is not None and event.provider_message_id not in self._messages:
            self._messages[event.provider_message_id] = _StoredMessage(
                raw=event.raw,
                full_text=event.raw.text,
                mailbox_id=event.mailbox_id,
                available_at=event.received_at,
            )
        return event

    async def fetch_message(self, provider_message_id: str) -> RawMessage:
        if self.fail_next_fetch is not None:
            failure = self.fail_next_fetch
            self.fail_next_fetch = None
            raise failure
        stored = self._messages.get(provider_message_id)
        if stored is None:
            raise MessageNotFoundError("fake provider has no such message")
        return RawMessage(
            provider_message_id=stored.raw.provider_message_id,
            mailbox_id=stored.mailbox_id,
            sender=stored.raw.sender,
            subject=stored.raw.subject,
            text=stored.full_text,
            received_at=stored.raw.received_at,
            thread_id=stored.raw.thread_id,
            attachment_count=stored.raw.attachment_count,
            withheld=False,
            headers=stored.raw.headers,
        )

    async def claim(
        self, mailbox_id: str, lease_seconds: int, consumer: str
    ) -> list[ClaimedMessage]:
        if self.fail_next_claim is not None:
            failure = self.fail_next_claim
            self.fail_next_claim = None
            raise failure

        now = self._now()
        claimed: list[ClaimedMessage] = []
        for stored in self._messages.values():
            if len(claimed) >= self._page_size:
                break
            if stored.mailbox_id != mailbox_id:
                continue
            if stored.acked_at is not None or stored.dead_letter:
                continue
            if stored.available_at > now:
                continue
            if stored.lease_until is not None and stored.lease_until > now:
                continue
            if stored.delivery_count >= self._max_claims:
                stored.dead_letter = True
                continue

            self._lease_counter += 1
            stored.delivery_count += 1
            stored.lease_id = f"fake-lease-{self._lease_counter}"
            stored.lease_until = now + timedelta(seconds=lease_seconds)
            claimed.append(
                ClaimedMessage(
                    lease_id=stored.lease_id,
                    lease_until=stored.lease_until,
                    delivery_count=stored.delivery_count,
                    message=RawMessage(
                        provider_message_id=stored.raw.provider_message_id,
                        mailbox_id=stored.mailbox_id,
                        sender=stored.raw.sender,
                        subject=stored.raw.subject,
                        text=stored.full_text,
                        received_at=stored.raw.received_at,
                        thread_id=stored.raw.thread_id,
                        attachment_count=stored.raw.attachment_count,
                        withheld=False,
                        headers=stored.raw.headers,
                    ),
                )
            )
        logger_consumer = consumer
        del logger_consumer
        return claimed

    async def ack(self, provider_message_id: str, lease_id: str) -> AckReceipt:
        if self.fail_next_ack is not None:
            failure = self.fail_next_ack
            self.fail_next_ack = None
            raise failure
        stored = self._messages.get(provider_message_id)
        if stored is None:
            raise MessageNotFoundError("fake provider has no such message")
        if stored.acked_at is not None:
            return AckReceipt(message_id=provider_message_id, acked_at=stored.acked_at)
        if stored.lease_id is None or stored.lease_id != lease_id:
            raise LeaseExpiredError("fake lease expired")
        stored.acked_at = self._now()
        stored.lease_id = None
        stored.lease_until = None
        return AckReceipt(message_id=provider_message_id, acked_at=stored.acked_at)

    async def nack(
        self, provider_message_id: str, lease_id: str, delay_seconds: int
    ) -> NackReceipt:
        if self.fail_next_nack is not None:
            failure = self.fail_next_nack
            self.fail_next_nack = None
            raise failure
        stored = self._messages.get(provider_message_id)
        if stored is None:
            raise MessageNotFoundError("fake provider has no such message")
        if stored.lease_id is None or stored.lease_id != lease_id:
            raise LeaseExpiredError("fake lease expired")
        stored.lease_id = None
        stored.lease_until = None
        if stored.delivery_count >= self._max_claims:
            stored.dead_letter = True
        stored.available_at = self._now() + timedelta(seconds=delay_seconds)
        return NackReceipt(
            message_id=provider_message_id,
            delivery_count=stored.delivery_count,
            available_at=stored.available_at,
            dead_letter=stored.dead_letter,
        )

    async def send(self, draft: OutboundDraft, idempotency_key: str) -> SentReceipt:
        if draft.in_reply_to is not None:
            raise ValueError("send() cannot set in_reply_to; use reply()")
        return self._deliver(draft, idempotency_key)

    async def reply(
        self,
        provider_message_id: str,
        draft: OutboundDraft,
        idempotency_key: str,
    ) -> SentReceipt:
        if draft.in_reply_to is not None and draft.in_reply_to != provider_message_id:
            raise ValueError("reply() target does not match in_reply_to")
        if provider_message_id not in self._messages:
            raise MessageNotFoundError("fake provider has no such message")
        return self._deliver(
            OutboundDraft(
                mailbox_id=draft.mailbox_id,
                to=draft.to,
                subject=draft.subject,
                text=draft.text,
                in_reply_to=provider_message_id,
            ),
            idempotency_key,
        )

    def _deliver(self, draft: OutboundDraft, idempotency_key: str) -> SentReceipt:
        existing = self._deliveries.get(idempotency_key)
        if existing is not None:
            return existing.receipt
        if self.fail_next_send is not None:
            failure = self.fail_next_send
            self.fail_next_send = None
            raise failure
        if draft.mailbox_id not in self._mailboxes:
            raise MessageNotFoundError("fake provider has no such mailbox")

        self._send_counter += 1
        receipt = SentReceipt(
            provider_message_id=f"fake-out-{self._send_counter}",
            status="queued",
            idempotency_key=idempotency_key,
        )
        self._deliveries[idempotency_key] = SentDelivery(
            draft=draft,
            idempotency_key=idempotency_key,
            receipt=receipt,
        )
        return receipt

    async def aclose(self) -> None:
        return None


def provider_unavailable(message: str = "fake provider unavailable") -> ProviderUnavailableError:
    """Convenience factory for fault-injection tests."""
    return ProviderUnavailableError(message, retryable=True)


def _normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


class FakeIntelligenceProvider:
    """Deterministic extraction and critique from registered fixtures.

    A fixture is selected by the first marker found in the message subject or
    body. Unmatched messages fail loudly; the fake never invents a report.
    Critique verdicts are consumed from ``verdicts`` in order (the last one
    repeats); ``verdict`` is shorthand for a single repeating verdict.
    """

    def __init__(
        self,
        fixtures: Mapping[str, ParsedEmail],
        *,
        verdict: CriticVerdict | None = None,
        verdicts: list[CriticVerdict] | None = None,
    ) -> None:
        if not fixtures:
            raise ValueError("at least one extraction fixture is required")
        self._fixtures = dict(fixtures)
        if verdicts:
            self._verdicts = list(verdicts)
        elif verdict is not None:
            self._verdicts = [verdict]
        else:
            self._verdicts = [CriticVerdict(verdict="approve")]
        self.fail_next_extract: AdapterError | None = None
        self.fail_next_critique: AdapterError | None = None
        self.critique_calls: list[CriticContext] = []

    async def extract(self, message: RawMessage) -> ParsedEmail:
        if self.fail_next_extract is not None:
            failure = self.fail_next_extract
            self.fail_next_extract = None
            raise failure
        haystack = f"{message.subject}\n{message.text}".casefold()
        for marker, parsed in self._fixtures.items():
            if marker.casefold() in haystack:
                return parsed.model_copy(deep=True)
        raise ExtractionError("no deterministic extraction fixture matched")

    def set_fixture(self, marker: str, parsed: ParsedEmail) -> None:
        """Add or replace one deterministic extraction fixture."""
        self._fixtures[marker] = parsed

    async def critique(self, proposal: CriticContext) -> CriticVerdict:
        self.critique_calls.append(proposal)
        if self.fail_next_critique is not None:
            failure = self.fail_next_critique
            self.fail_next_critique = None
            raise failure
        if len(self._verdicts) > 1:
            return self._verdicts.pop(0).model_copy(deep=True)
        return self._verdicts[0].model_copy(deep=True)


class FakeGeocoder:
    """Offline gazetteer: exact known places, otherwise no location.

    Matching is a deliberate substring fixture lookup, not production
    geocoding: quoted or negated text is out of scope for this fake.
    """

    def __init__(
        self,
        places: Mapping[str, tuple[Decimal, Decimal]],
        *,
        source: str = "gazetteer",
    ) -> None:
        self._places = {
            _normalize_text(name): (lat, lon) for name, (lat, lon) in places.items()
        }
        self._source = source
        self.fail_next_geocode: AdapterError | None = None

    async def geocode(self, incident_id: UUID, location_text: str) -> GeoResult:
        if self.fail_next_geocode is not None:
            failure = self.fail_next_geocode
            self.fail_next_geocode = None
            raise failure
        normalized = _normalize_text(location_text)
        if not normalized:
            return GeoResult(lat=None, lon=None, confidence=GeoConfidence.NONE, source="none")
        for name, (lat, lon) in sorted(
            self._places.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if name in normalized:
                return GeoResult(
                    lat=lat,
                    lon=lon,
                    confidence=GeoConfidence.EXACT,
                    source=self._source,
                )
        return GeoResult(
            lat=None, lon=None, confidence=GeoConfidence.NONE, source=self._source
        )


@dataclass(frozen=True, slots=True)
class FakeMagicLink:
    email: str
    redirect_url: str
    state_id: str


class FakeAuthProvider:
    """Deterministic MojoAuth-shaped magic-link provider.

    ``sent`` holds successfully requested links, ``attempts`` every provider
    call (including failures), and ``fail_next_send`` / ``fail_next_verify``
    inject the same errors a live adapter raises.
    """

    def __init__(self) -> None:
        self.sent: list[FakeMagicLink] = []
        self.attempts: list[str] = []
        self.fail_next_send: AdapterError | None = None
        self.fail_next_verify: AdapterError | None = None
        self.next_email_override: str | None = None
        self.next_verified_override: bool | None = None
        self._states: dict[str, str] = {}
        self._counter = 0

    async def send_magic_link(
        self, email: str, redirect_url: str
    ) -> VerificationRequest:
        self.attempts.append(email)
        if self.fail_next_send is not None:
            failure = self.fail_next_send
            self.fail_next_send = None
            raise failure
        self._counter += 1
        state_id = f"state-{self._counter}"
        self._states[state_id] = email
        self.sent.append(
            FakeMagicLink(email=email, redirect_url=redirect_url, state_id=state_id)
        )
        return VerificationRequest(state_id=state_id, status="success")

    async def verify_callback(self, params: Mapping[str, str]) -> VerifiedIdentity:
        if self.fail_next_verify is not None:
            failure = self.fail_next_verify
            self.fail_next_verify = None
            raise failure
        state_id = str(params.get("state_id", ""))
        email = self._states.get(state_id)
        if not email:
            raise AdapterError("unknown or missing verification state")
        if self.next_email_override is not None:
            email = self.next_email_override
        verified = (
            True if self.next_verified_override is None else self.next_verified_override
        )
        return VerifiedIdentity(
            email=email, verified=verified, provider_subject=f"subject-{state_id}"
        )


__all__ = [
    "FakeAuthProvider",
    "FakeGeocoder",
    "FakeInboxProvider",
    "FakeIntelligenceProvider",
    "FakeMagicLink",
    "SentDelivery",
    "provider_unavailable",
]

"""Task 13: MojoAuth verification and server-side sessions.

Fake-provider integration tests prove the contract ResiliNet controls: one
idempotent verification request per sender, binding to the provider state,
a hashed server-side session, CSRF, role checks that verification never
grants, and graceful behaviour when the provider is unavailable. No live
MojoAuth account is used or claimed.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.adapters.fakes import (
    FakeAuthProvider,
    FakeGeocoder,
    FakeInboxProvider,
    FakeIntelligenceProvider,
)
from resilinet.config import Settings
from resilinet.db import transaction
from resilinet.domain.quantities import QuantitySource
from resilinet.main import Container, create_app
from resilinet.models import (
    AuthSession,
    Entity,
    JobType,
    Message,
    Need,
    ParticipantRole,
    ParticipantRoleKind,
    Report,
    Sender,
    VerificationRequest,
    VerificationStatus,
)
from resilinet.ports import InboundEvent, ProviderUnavailableError, RawMessage
from resilinet.schemas import (
    EntityType,
    FieldConfidence,
    ParsedEmail,
    ParsedReport,
    ReportContext,
    ReportKind,
    Resource,
)
from resilinet.security import (
    AUTH_NONCE_COOKIE,
    SESSION_COOKIE_NAME,
    derive_csrf_token,
    hash_token,
)
from resilinet.services.extraction import ExtractionService
from resilinet.services.identity import IdentityService
from resilinet.services.ingestion import IngestionService
from resilinet.services.notifications import NotificationService
from resilinet.workers.handlers import MessageJobHandler
from resilinet.workers.runner import DurableWorker

from .conftest import NOW, Clock

SETTINGS = Settings(
    app_env="test",
    incident_key="demo-incident",
    database_url="sqlite+aiosqlite:///:memory:",
    public_base_url="http://testserver",
    _env_file=None,
)
PLACES = {"Gandhi Park": (Decimal("22.5726"), Decimal("88.3639"))}
CONFIDENCE = FieldConfidence(location=0.95, items=0.95, headcount=0.95)


def make_service(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    auth: FakeAuthProvider,
) -> IdentityService:
    return IdentityService(session_factory, auth, SETTINGS, clock=clock)


def make_app(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    auth: FakeAuthProvider,
    settings: Settings | None = None,
) -> FastAPI:
    resolved_settings = settings or SETTINGS
    inbox = FakeInboxProvider(webhook_secret="secret", now=clock)
    container = Container(
        settings=resolved_settings,
        session_factory=session_factory,
        inbox=inbox,
        auth=auth,
        clock=clock,
    )
    return create_app(resolved_settings, container)


async def make_sender(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    address: str = "volunteer@example.org",
    *,
    verified: bool = False,
) -> Sender:
    async with transaction(session_factory) as session:
        sender = Sender(
            incident_id=incident_id,
            address=address,
            verified=verified,
            verified_at=NOW if verified else None,
        )
        session.add(sender)
        await session.flush()
        return sender


async def add_role(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    sender_id: UUID,
    role: ParticipantRoleKind,
    entity_id: UUID | None = None,
) -> None:
    async with transaction(session_factory) as session:
        session.add(
            ParticipantRole(
                incident_id=incident_id,
                sender_id=sender_id,
                role=role,
                entity_id=entity_id,
            )
        )
        await session.flush()


async def make_need_for_sender(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    sender_id: UUID,
) -> UUID:
    async with transaction(session_factory) as session:
        entity = Entity(
            incident_id=incident_id,
            name="Shelter 4",
            normalized_name="shelter 4",
            entity_type=EntityType.SHELTER,
            lat=22.5726,
            lon=88.3639,
        )
        session.add(entity)
        await session.flush()
        message = Message(
            incident_id=incident_id,
            sender_id=sender_id,
            provider_message_id="msg-1",
            mailbox_id="mb-1",
            subject="Water",
            body_text="We need water",
            received_at=NOW,
        )
        session.add(message)
        await session.flush()
        report = Report(
            incident_id=incident_id,
            message_id=message.id,
            sender_id=sender_id,
            entity_id=entity.id,
            kind=ReportKind.NEED,
            resource=Resource.POTABLE_WATER,
            quantity=100,
            unit="l",
        )
        session.add(report)
        await session.flush()
        need = Need(
            incident_id=incident_id,
            entity_id=entity.id,
            source_report_id=report.id,
            ref="RN-100001",
            resource=Resource.POTABLE_WATER,
            quantity=100,
            unit="l",
            quantity_source=QuantitySource.STATED,
            verified=False,
        )
        session.add(need)
        await session.flush()
        return need.id


async def test_one_pending_verification_per_sender(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    service = make_service(session_factory, clock, auth)
    sender = await make_sender(session_factory, incident_id)

    first = await service.ensure_sender_verification(sender.id)
    second = await service.ensure_sender_verification(sender.id)

    assert first is not None and second is not None
    assert first.id == second.id
    assert len(auth.sent) == 1
    assert first.provider_state_id is not None
    assert first.status is VerificationStatus.PENDING

    async with session_factory() as session:
        rows = list(await session.scalars(sa.select(VerificationRequest)))
    assert len(rows) == 1


async def test_one_send_claim_prevents_a_duplicate_magic_link(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    service = make_service(session_factory, clock, auth)
    sender = await make_sender(session_factory, incident_id)
    request = await service.ensure_sender_verification(sender.id)
    assert request is not None and request.provider_state_id is not None
    async with transaction(session_factory) as session:
        row = await session.get(VerificationRequest, request.id)
        assert row is not None
        # Simulate a sender that is mid-flight: no state yet, fresh claim.
        row.provider_state_id = None
        row.send_claimed_at = clock.now
    duplicate = await service.ensure_sender_verification(sender.id)
    assert duplicate is not None
    assert duplicate.provider_state_id is None
    assert len(auth.attempts) == 1

    clock.advance(minutes=3)
    retried = await service.ensure_sender_verification(sender.id)
    assert retried is not None
    assert retried.provider_state_id is not None
    assert len(auth.attempts) == 2


async def test_provider_failure_marks_failed_and_next_attempt_can_retry(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    auth.fail_next_send = ProviderUnavailableError("auth provider down")
    service = make_service(session_factory, clock, auth)
    sender = await make_sender(session_factory, incident_id)

    failed = await service.ensure_sender_verification(sender.id)
    assert failed is not None
    assert failed.status is VerificationStatus.FAILED

    retried = await service.ensure_sender_verification(sender.id)
    assert retried is not None
    assert retried.status is VerificationStatus.PENDING
    assert retried.id != failed.id
    assert len(auth.sent) == 1


async def test_callback_creates_a_session_and_upgrades_prior_needs(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    service = make_service(session_factory, clock, auth)
    sender = await make_sender(session_factory, incident_id)
    need_id = await make_need_for_sender(session_factory, incident_id, sender.id)

    request = await service.ensure_sender_verification(sender.id)
    assert request is not None and request.provider_state_id is not None
    grant = await service.complete_callback({"state_id": request.provider_state_id})
    assert grant is not None
    assert grant.redirect_path == "/"

    principal = await service.resolve_session(grant.cookie_token)
    assert principal is not None
    assert principal.sender_id == sender.id
    assert principal.incident_id == incident_id

    async with session_factory() as session:
        sender_row = await session.get(Sender, sender.id)
        need_row = await session.get(Need, need_id)
        request_row = await session.get(VerificationRequest, request.id)
        session_count = await session.scalar(
            sa.select(sa.func.count()).select_from(AuthSession)
        )
    assert sender_row is not None and sender_row.verified is True
    assert sender_row.verified_at == NOW
    assert need_row is not None and need_row.verified is True
    assert request_row is not None
    assert request_row.status is VerificationStatus.COMPLETED
    assert session_count == 1


async def test_callback_rejects_unknown_expired_replayed_and_wrong_email(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    service = make_service(session_factory, clock, auth)
    sender = await make_sender(session_factory, incident_id)

    assert await service.complete_callback({"state_id": "unknown"}) is None

    request = await service.ensure_sender_verification(sender.id)
    assert request is not None and request.provider_state_id is not None
    state = request.provider_state_id

    clock.advance(minutes=31)
    assert await service.complete_callback({"state_id": state}) is None
    async with session_factory() as session:
        expired = await session.get(VerificationRequest, request.id)
        assert expired is not None
        assert expired.status is VerificationStatus.EXPIRED
        sender_row = await session.get(Sender, sender.id)
        assert sender_row is not None and sender_row.verified is False

    clock.now = NOW
    fresh = await service.ensure_sender_verification(sender.id)
    assert fresh is not None and fresh.provider_state_id is not None
    auth.next_email_override = "attacker@example.org"
    wrong = await service.complete_callback({"state_id": fresh.provider_state_id})
    assert wrong is None  # bound address mismatch is refused, not silently accepted
    auth.next_email_override = None

    service2 = make_service(session_factory, clock, auth)
    request2 = await service2.ensure_sender_verification(sender.id)
    assert request2 is not None and request2.provider_state_id is not None
    ok = await service2.complete_callback({"state_id": request2.provider_state_id})
    assert ok is not None
    replay = await service2.complete_callback({"state_id": request2.provider_state_id})
    assert replay is None


async def test_logout_requires_csrf_and_revokes_the_session(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    service = make_service(session_factory, clock, auth)
    sender = await make_sender(session_factory, incident_id)
    request = await service.ensure_sender_verification(sender.id)
    assert request is not None and request.provider_state_id is not None
    grant = await service.complete_callback({"state_id": request.provider_state_id})
    assert grant is not None

    assert await service.logout(grant.cookie_token, "wrong") is False
    assert await service.resolve_session(grant.cookie_token) is not None
    assert await service.logout(grant.cookie_token, grant.csrf_token) is True
    assert await service.resolve_session(grant.cookie_token) is None


async def test_session_expiry_is_enforced(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    service = make_service(session_factory, clock, auth)
    sender = await make_sender(session_factory, incident_id)
    request = await service.ensure_sender_verification(sender.id)
    assert request is not None and request.provider_state_id is not None
    grant = await service.complete_callback({"state_id": request.provider_state_id})
    assert grant is not None
    assert grant.expires_at > clock.now

    clock.advance(hours=9)
    assert await service.resolve_session(grant.cookie_token) is None


async def test_operator_login_callback_session_and_role_check(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    operator = await make_sender(session_factory, incident_id, "ops@example.org")
    await add_role(session_factory, incident_id, operator.id, ParticipantRoleKind.OPERATOR)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post("/operator/login", data={"email": "ops@example.org"})
        assert login.status_code == 200
        assert "verification" in login.text.lower()
        assert len(auth.sent) == 1
        state = auth.sent[0].state_id

        page = await client.get("/auth/callback", params={"state_id": state})
        assert page.status_code == 200
        assert "Continue" in page.text
        nonce = client.cookies.get(AUTH_NONCE_COOKIE)
        assert nonce is not None
        callback = await client.post(
            "/auth/callback",
            data={"state_id": state, "nonce": nonce},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == "/operator/reviews"
        cookie = callback.headers["set-cookie"]
        assert f"{SESSION_COOKIE_NAME}=" in cookie
        assert "HttpOnly" in cookie
        assert "samesite=lax" in cookie.lower()
        assert "max-age=" in cookie.lower()
        assert "; secure" not in cookie.lower()  # test mode is plain HTTP
        assert "state_id" not in callback.headers["location"]

        page = await client.get("/operator/reviews")
        assert page.status_code == 200

        token = client.cookies.get(SESSION_COOKIE_NAME)
        assert token is not None
        csrf = derive_csrf_token(hash_token(token))
        logout = await client.post(
            "/auth/logout", data={"csrf_token": csrf}, follow_redirects=False
        )
        assert logout.status_code == 303
        denied = await client.get("/operator/reviews", follow_redirects=False)
        assert denied.status_code in (401, 403, 303)


async def test_callback_get_previews_without_consuming_or_creating_a_session(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    sender = await make_sender(session_factory, incident_id)
    service = make_service(session_factory, clock, auth)
    request = await service.ensure_sender_verification(sender.id)
    assert request is not None and request.provider_state_id is not None
    state = request.provider_state_id

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for _ in range(2):
            preview = await client.get(
                "/auth/callback", params={"state_id": state}
            )
            assert preview.status_code == 200
            assert "Continue" in preview.text
        async with session_factory() as session:
            row = await session.get(VerificationRequest, request.id)
            sessions = await session.scalar(
                sa.select(sa.func.count()).select_from(AuthSession)
            )
            sender_row = await session.get(Sender, sender.id)
        assert row is not None and row.status is VerificationStatus.PENDING
        assert sessions == 0
        assert sender_row is not None and sender_row.verified is False
        assert client.cookies.get(SESSION_COOKIE_NAME) is None

        missing_nonce = await client.post(
            "/auth/callback", data={"state_id": state}, follow_redirects=False
        )
        assert missing_nonce.status_code == 400
        wrong_nonce = await client.post(
            "/auth/callback",
            data={"state_id": state, "nonce": "wrong"},
            follow_redirects=False,
        )
        assert wrong_nonce.status_code == 400
        async with session_factory() as session:
            row = await session.get(VerificationRequest, request.id)
            sessions = await session.scalar(
                sa.select(sa.func.count()).select_from(AuthSession)
            )
        assert row is not None and row.status is VerificationStatus.PENDING
        assert sessions == 0

        nonce = client.cookies.get(AUTH_NONCE_COOKIE)
        assert nonce is not None
        completed = await client.post(
            "/auth/callback",
            data={"state_id": state, "nonce": nonce},
            follow_redirects=False,
        )
        assert completed.status_code == 303
        assert client.cookies.get(SESSION_COOKIE_NAME) is not None
        replay = await client.get("/auth/callback", params={"state_id": state})
        assert replay.status_code == 400


async def test_verification_does_not_grant_operator_or_depot_authority(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    await make_sender(session_factory, incident_id, "ordinary@example.org")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await client.post("/operator/login", data={"email": "ordinary@example.org"})
        state = auth.sent[0].state_id
        page = await client.get("/auth/callback", params={"state_id": state})
        assert page.status_code == 200
        nonce = client.cookies.get(AUTH_NONCE_COOKIE)
        assert nonce is not None
        callback = await client.post(
            "/auth/callback",
            data={"state_id": state, "nonce": nonce},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        # An ordinary verified sender has a session but no operator role.
        denied = await client.get("/operator/reviews", follow_redirects=False)
        assert denied.status_code == 403


async def test_unknown_address_never_receives_a_magic_link_or_creates_a_sender(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/operator/login", data={"email": "stranger@example.org"}
        )
        assert response.status_code == 200
        assert "verification" in response.text.lower()
    assert auth.attempts == []
    assert auth.sent == []
    async with session_factory() as session:
        count = await session.scalar(sa.select(sa.func.count(Sender.id)))
    assert count == 0


async def test_global_operator_login_throttle_bounds_rotated_addresses(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    settings = Settings(
        app_env="test",
        incident_key="demo-incident",
        database_url="sqlite+aiosqlite:///:memory:",
        public_base_url="http://testserver",
        operator_login_global_max_requests=1,
        operator_login_max_requests=10,
        _env_file=None,
    )
    await make_sender(session_factory, incident_id, "one@example.org")
    await make_sender(session_factory, incident_id, "two@example.org")
    app = make_app(session_factory, clock, auth, settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post("/operator/login", data={"email": "one@example.org"})
        second = await client.post("/operator/login", data={"email": "two@example.org"})
        assert first.status_code == 200 and second.status_code == 200
        assert second.text == first.text
    assert auth.attempts == ["one@example.org"]
    assert len(auth.sent) == 1


async def test_operator_login_is_throttled(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    app = make_app(session_factory, clock, auth)
    await make_sender(session_factory, incident_id, "ops@example.org")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for _ in range(6):
            response = await client.post(
                "/operator/login", data={"email": "ops@example.org"}
            )
            assert response.status_code == 200
            assert "verification" in response.text.lower()
            # Complete the outstanding request so the next call would otherwise
            # be allowed to send a new link.
            if auth.sent:
                await client.get(
                    "/auth/callback",
                    params={"state_id": auth.sent[-1].state_id},
                )
                nonce = client.cookies.get(AUTH_NONCE_COOKIE)
                assert nonce is not None
                await client.post(
                    "/auth/callback",
                    data={"state_id": auth.sent[-1].state_id, "nonce": nonce},
                    follow_redirects=False,
                )
        assert len(auth.attempts) <= SETTINGS.operator_login_max_requests


async def test_worker_requests_verification_without_blocking_processing(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    """The durable worker requests one verification and still processes mail."""
    auth = FakeAuthProvider()
    inbox = FakeInboxProvider(webhook_secret="secret", now=clock)
    mailbox = await inbox.provision_inbox("demo-incident")
    parsed = ParsedEmail(
        context=ReportContext(
            kind=ReportKind.NEED,
            entity_name="Shelter 4",
            entity_type=EntityType.SHELTER,
            location_text="Gandhi Park",
            confidence=CONFIDENCE,
        ),
        reports=[ParsedReport(resource=Resource.POTABLE_WATER, quantity=10, unit="l")],
    )
    intelligence = FakeIntelligenceProvider({"water": parsed})
    identity = IdentityService(session_factory, auth, SETTINGS, clock=clock)
    handler = MessageJobHandler(
        session_factory=session_factory,
        inbox=inbox,
        extraction=ExtractionService(incident_id, intelligence, FakeGeocoder(PLACES)),
        notifications=NotificationService(),
        intelligence=intelligence,
        identity=identity,
        public_base_url="http://testserver",
        clock=clock,
    )
    worker = DurableWorker(
        session_factory,
        {JobType.PROCESS_MESSAGE: handler, JobType.FETCH_CONTENT: handler},
        clock=clock,
        jitter=lambda: 0.0,
        worker_id="identity-worker",
    )
    ingestion = IngestionService(incident_id, session_factory, clock=clock)

    async def deliver(provider_message_id: str, sender: str) -> None:
        text = "Shelter 4 needs 10 l of water at Gandhi Park."
        inbox.deliver_inbound(
            mailbox_id=mailbox.id,
            provider_message_id=provider_message_id,
            sender=sender,
            subject="Water",
            text=text,
            thread_id=f"thread-{provider_message_id}",
            received_at=clock.now,
        )
        await ingestion.accept(
            InboundEvent(
                event_id=f"evt-{provider_message_id}",
                event_type="message.received",
                mailbox_id=mailbox.id,
                provider_message_id=provider_message_id,
                sender=sender,
                subject="Water",
                received_at=clock.now,
                withheld=False,
                mailbox_address="incident@example.org",
                thread_id=f"thread-{provider_message_id}",
                raw=RawMessage(
                    provider_message_id=provider_message_id,
                    mailbox_id=mailbox.id,
                    sender=sender,
                    subject="Water",
                    text=text,
                    received_at=clock.now,
                    thread_id=f"thread-{provider_message_id}",
                ),
            )
        )
        assert await worker.run_once() == 1

    await deliver("msg-1", "volunteer@example.org")
    await deliver("msg-2", "volunteer@example.org")

    assert len(auth.sent) == 1
    async with session_factory() as session:
        statuses = list(
            await session.scalars(sa.select(VerificationRequest.status))
        )
        message_statuses = list(await session.scalars(sa.select(Message.status)))
    assert statuses == [VerificationStatus.PENDING]
    assert all(status.value == "processed" for status in message_statuses)

    # Provider outage must not fail processing or retry forever.
    auth.fail_next_send = ProviderUnavailableError("auth provider down")
    await deliver("msg-3", "another@example.org")
    async with session_factory() as session:
        failed = await session.scalar(
            sa.select(VerificationRequest).where(
                VerificationRequest.status == VerificationStatus.FAILED
            )
        )
        messages = list(await session.scalars(sa.select(Message)))
    assert failed is not None
    assert len(messages) == 3
    assert all(message.status.value == "processed" for message in messages)


async def test_csrf_token_is_bound_to_the_session(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    auth = FakeAuthProvider()
    service = make_service(session_factory, clock, auth)
    sender = await make_sender(session_factory, incident_id)
    request = await service.ensure_sender_verification(sender.id)
    assert request is not None and request.provider_state_id is not None
    grant = await service.complete_callback({"state_id": request.provider_state_id})
    assert grant is not None
    principal = await service.resolve_session(grant.cookie_token)
    assert principal is not None
    assert principal.csrf_token == grant.csrf_token
    other = FakeAuthProvider()
    othersvc = make_service(session_factory, clock, other)
    other_sender = await make_sender(session_factory, incident_id, "other@example.org")
    other_req = await othersvc.ensure_sender_verification(other_sender.id)
    assert other_req is not None and other_req.provider_state_id is not None
    other_grant = await othersvc.complete_callback(
        {"state_id": other_req.provider_state_id}
    )
    assert other_grant is not None
    assert not await service.verify_csrf(principal, other_grant.csrf_token)
    assert await service.verify_csrf(principal, grant.csrf_token)


def test_live_cookies_are_secure() -> None:
    live = Settings(
        app_env="live",
        database_url="postgresql+asyncpg://user:pass@localhost/resilinet",
        agentboxd_api_key="key",
        agentboxd_webhook_secret="secret",
        mojoauth_api_key="key",
        openai_api_key="key",
        _env_file=None,
    )
    assert live.is_live
    from resilinet.security import session_cookie_secure

    assert session_cookie_secure(live) is True
    assert session_cookie_secure(SETTINGS) is False


def test_templates_directory_exists() -> None:
    assert (Path(__file__).resolve().parents[2] / "templates").is_dir()

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import httpx
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.adapters.agentboxd import sign_webhook
from resilinet.adapters.fakes import FakeInboxProvider
from resilinet.config import Settings
from resilinet.main import Container, create_app
from resilinet.models import Job, Message

from .conftest import NOW, Clock

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "agentboxd" / "webhook_received_full.json"
WEBHOOK_SECRET = "test-webhook-secret"


def signed_headers(body: bytes, clock: Clock, *, timestamp: int | None = None) -> dict[str, str]:
    stamp = timestamp if timestamp is not None else int(clock.now.timestamp())
    return {
        "X-Mailroom-Timestamp": str(stamp),
        "X-Mailroom-Signature": sign_webhook(body, WEBHOOK_SECRET, stamp),
        "Content-Type": "application/json",
    }


def make_app(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    incident_key: str = "demo-incident",
    configured: bool = True,
) -> FastAPI:
    settings = Settings(
        app_env="test",
        incident_key=incident_key,
        database_url="sqlite+aiosqlite:///:memory:",
        _env_file=None,
    )
    container = (
        Container(
            settings=settings,
            session_factory=session_factory,
            inbox=FakeInboxProvider(webhook_secret=WEBHOOK_SECRET, now=clock),
        )
        if configured
        else Container(settings=settings)
    )
    return create_app(settings, container)


async def post(app: FastAPI, body: bytes, headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/internal/agentboxd/events", content=body, headers=headers)


async def test_signed_webhook_is_accepted_once(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    body = FIXTURE.read_bytes()
    app = make_app(session_factory, clock)

    first = await post(app, body, signed_headers(body, clock))
    second = await post(app, body, signed_headers(body, clock))

    assert first.status_code == 204
    assert second.status_code == 204
    async with session_factory() as session:
        messages = await session.scalar(sa.select(sa.func.count(Message.id)))
        jobs = await session.scalar(sa.select(sa.func.count(Job.id)))
        row = await session.scalar(sa.select(Message))
        job = await session.scalar(sa.select(Job))
    assert messages == 1
    assert jobs == 1
    assert row is not None and row.provider_message_id == "msg_01"
    assert job is not None


async def test_tampered_body_is_rejected_before_parsing(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    signed = FIXTURE.read_bytes()
    tampered = signed.replace(b"Shelter 4 water", b"Shelter 9 water")
    app = make_app(session_factory, clock)

    response = await post(app, tampered, signed_headers(signed, clock))

    assert response.status_code == 400
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count(Message.id))) == 0


async def test_stale_timestamp_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    body = FIXTURE.read_bytes()
    app = make_app(session_factory, clock)
    stale = int(NOW.timestamp()) - 400

    response = await post(app, body, signed_headers(body, clock, timestamp=stale))

    assert response.status_code == 400


async def test_missing_signature_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    body = FIXTURE.read_bytes()
    app = make_app(session_factory, clock)

    response = await post(app, body, {"Content-Type": "application/json"})

    assert response.status_code == 400


async def test_unconfigured_webhook_returns_503(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    body = FIXTURE.read_bytes()
    app = make_app(session_factory, clock, configured=False)

    response = await post(app, body, signed_headers(body, clock))

    assert response.status_code == 503


async def test_unknown_incident_returns_404(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    body = FIXTURE.read_bytes()
    app = make_app(session_factory, clock, incident_key="not-provisioned")

    response = await post(app, body, signed_headers(body, clock))

    assert response.status_code == 404


async def test_non_inbound_event_is_acknowledged_without_ingestion(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    body = json.dumps(
        {"id": "evt-sent", "type": "message.sent", "data": {}}
    ).encode("utf-8")
    app = make_app(session_factory, clock)

    response = await post(app, body, signed_headers(body, clock))

    assert response.status_code == 204
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count(Message.id))) == 0
        assert await session.scalar(sa.select(sa.func.count(Job.id))) == 0

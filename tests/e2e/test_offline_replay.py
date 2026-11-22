"""Task 23: the full demo replays with the network disabled.

A fresh app in replay mode must reject live provider construction (even
when keys are present), run need/supply/duplicate/injection/medical
scenarios from the recorded extracts, and serve map/table/lite over
loopback — with zero attempted non-loopback socket connections.
"""

from __future__ import annotations

import json
import socket
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.adapters.fakes import (
    FakeAuthProvider,
    FakeGeocoder,
    FakeInboxProvider,
    FakeIntelligenceProvider,
)
from resilinet.composition import (
    build_auth_provider,
    build_inbox_provider,
    build_intelligence_provider,
)
from resilinet.config import Settings
from resilinet.main import Container, create_app
from resilinet.models import JobType, Need, ReviewCase, ReviewCaseKind
from resilinet.services.actions import ActionService
from resilinet.services.allocations import AllocationService
from resilinet.services.extraction import ExtractionService
from resilinet.services.ingestion import IngestionService
from resilinet.services.notifications import NotificationService, OutboxService
from resilinet.workers.handlers import MessageJobHandler
from resilinet.workers.runner import DurableWorker

from .conftest import Clock
from .test_golden_path import Harness, deliver

ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "http://testserver"


@pytest.fixture
def socket_guard(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Block and record every non-loopback socket connection attempt."""
    attempts: list[str] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def host_of(address: object) -> str:
        if isinstance(address, tuple) and address:
            return str(address[0])
        return str(address)

    def guarded_connect(self, address, *args, **kwargs):  # type: ignore[no-untyped-def]
        host = host_of(address)
        if host in {"127.0.0.1", "::1", "localhost"}:
            return real_connect(self, address, *args, **kwargs)
        attempts.append(host)
        raise OSError(f"offline replay blocked connection to {host}")

    def guarded_connect_ex(self, address, *args, **kwargs):  # type: ignore[no-untyped-def]
        host = host_of(address)
        if host in {"127.0.0.1", "::1", "localhost"}:
            return real_connect_ex(self, address, *args, **kwargs)
        attempts.append(host)
        return 111  # ECONNREFUSED without touching the network

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    return attempts


def replay_settings() -> Settings:
    # Keys are present on purpose: replay must refuse to use them.
    return Settings(
        app_env="replay",
        incident_key="demo-incident",
        database_url="sqlite+aiosqlite:///:memory:",
        public_base_url=BASE_URL,
        nvidia_api_key="replay-must-not-use-this",
        mojoauth_api_key="replay-must-not-use-this",
        _env_file=None,
    )


def corpus_message(message_id: str) -> dict:
    data = json.loads((ROOT / "fixtures/messages.json").read_text(encoding="utf-8"))
    return next(item for item in data["messages"] if item["id"] == message_id)


def test_replay_composition_never_builds_live_providers() -> None:
    settings = replay_settings()
    intelligence = build_intelligence_provider(settings)
    assert isinstance(intelligence, FakeIntelligenceProvider)
    assert isinstance(build_inbox_provider(settings), FakeInboxProvider)
    assert build_auth_provider(settings) is None


async def test_offline_replay_end_to_end(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
    socket_guard: list[str],
) -> None:
    settings = replay_settings()
    intelligence = build_intelligence_provider(settings)
    assert isinstance(intelligence, FakeIntelligenceProvider)
    inbox = FakeInboxProvider(webhook_secret="secret", now=clock)
    mailbox = await inbox.provision_inbox("demo-incident")
    places = {
        place["name"]: (Decimal(place["lat"]), Decimal(place["lon"]))
        for place in json.loads(
            (ROOT / "fixtures/gazetteer.json").read_text(encoding="utf-8")
        )["places"]
    }
    handler = MessageJobHandler(
        session_factory=session_factory,
        inbox=inbox,
        extraction=ExtractionService(incident_id, intelligence, FakeGeocoder(places)),
        notifications=NotificationService(),
        intelligence=intelligence,
        public_base_url=BASE_URL,
        clock=clock,
    )
    worker = DurableWorker(
        session_factory,
        {JobType.PROCESS_MESSAGE: handler, JobType.FETCH_CONTENT: handler},
        clock=clock,
        jitter=lambda: 0.0,
        worker_id="replay-worker",
    )
    harness = Harness(
        inbox=inbox,
        mailbox_id=mailbox.id,
        ingestion=IngestionService(incident_id, session_factory, clock=clock),
        worker=worker,
        outbox=OutboxService(session_factory, inbox, clock=clock),
        intelligence=intelligence,
        session_factory=session_factory,
        incident_id=incident_id,
        clock=clock,
        action_service=ActionService(
            AllocationService(clock=clock), NotificationService(), clock=clock
        ),
        auth=FakeAuthProvider(),
    )

    # Need, supply, near-duplicate, medical, injection — from the corpus.
    for message_id in ("M01", "M11", "M26", "M06", "M29"):
        item = corpus_message(message_id)
        await deliver(
            harness,
            provider_message_id=f"replay-{message_id.lower()}",
            subject=item["subject"],
            text=item["body"],
            sender=item["sender"],
        )

    async with session_factory() as session:
        needs = int(
            await session.scalar(
                sa.select(sa.func.count(Need.id)).where(Need.incident_id == incident_id)
            )
            or 0
        )
        medical_reviews = int(
            await session.scalar(
                sa.select(sa.func.count(ReviewCase.id)).where(
                    ReviewCase.incident_id == incident_id,
                    ReviewCase.kind == ReviewCaseKind.MEDICAL,
                )
            )
            or 0
        )
        need_refs = list(
            await session.scalars(
                sa.select(Need.ref).where(Need.incident_id == incident_id)
            )
        )
    assert needs == 2, "M01 + M06 create needs; M26 corroborates; M29 creates none"
    assert medical_reviews >= 1, "the medical report must escalate for review"
    assert inbox.sent_messages, "replies should be recorded by the fake inbox"
    assert all(
        "agentboxd" not in sent.draft.text.casefold() for sent in inbox.sent_messages
    ), "replayed email must never be labelled as sent by the live provider"

    container = Container(
        settings=settings, session_factory=session_factory, clock=clock, inbox=inbox
    )
    app = create_app(settings, container)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        public_map = await client.get(f"/api/v1/incidents/{incident_id}/map")
        replay = await client.get("/replay", params={"incident_id": str(incident_id)})
        lite = await client.get("/replay/lite", params={"incident_id": str(incident_id)})

    assert public_map.status_code == 200
    assert need_refs and need_refs[0] in public_map.text
    assert replay.status_code == 200
    assert "Replay" in replay.text and "not live" in replay.text.casefold()
    assert lite.status_code == 200
    assert need_refs[0] in lite.text

    assert socket_guard == [], f"external connections attempted: {socket_guard}"

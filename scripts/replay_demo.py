"""Replay the full demo corpus with the network disabled.

Examples:

    uv run python scripts/replay_demo.py
    uv run python scripts/replay_demo.py --report /tmp/replay.json

The app factory runs in replay mode (recorded fakes only — live provider
construction is refused even though no keys are configured here), the
30-message corpus is processed by the durable worker, and the public map,
replay mirror, and lite view are read back over the ASGI transport. A
socket guard records every non-loopback connection attempt; the run fails
if any is made. Timing is the local pipeline's, never a sponsor's.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import tempfile
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median

import httpx
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from seed_demo import seed_demo  # noqa: E402

from resilinet.adapters.fakes import FakeGeocoder  # noqa: E402
from resilinet.composition import build_inbox_provider, build_intelligence_provider  # noqa: E402
from resilinet.config import Settings  # noqa: E402
from resilinet.db import create_engine, create_session_factory  # noqa: E402
from resilinet.main import Container, create_app  # noqa: E402
from resilinet.models import Incident, JobType, Need  # noqa: E402
from resilinet.ports import InboundEvent, RawMessage  # noqa: E402
from resilinet.services.extraction import ExtractionService  # noqa: E402
from resilinet.services.ingestion import IngestionService  # noqa: E402
from resilinet.services.notifications import NotificationService, OutboxService  # noqa: E402
from resilinet.workers.handlers import MessageJobHandler  # noqa: E402
from resilinet.workers.runner import DurableWorker  # noqa: E402

NOW = datetime(2026, 11, 22, 9, 0, 0, tzinfo=UTC)
ATTEMPTS: list[str] = []


class Clock:
    def __call__(self) -> datetime:
        return NOW


def install_socket_guard() -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def host_of(address: object) -> str:
        return str(address[0]) if isinstance(address, tuple) and address else str(address)

    def guarded_connect(self, address, *args, **kwargs):  # type: ignore[no-untyped-def]
        host = host_of(address)
        if host in {"127.0.0.1", "::1", "localhost"}:
            return real_connect(self, address, *args, **kwargs)
        ATTEMPTS.append(host)
        raise OSError(f"replay blocked connection to {host}")

    def guarded_connect_ex(self, address, *args, **kwargs):  # type: ignore[no-untyped-def]
        host = host_of(address)
        if host in {"127.0.0.1", "::1", "localhost"}:
            return real_connect_ex(self, address, *args, **kwargs)
        ATTEMPTS.append(host)
        return 111

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]


def upgrade_database(path: Path) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, "head")


async def run(db_path: Path) -> dict:
    settings = Settings(
        app_env="replay",
        database_url=f"sqlite+aiosqlite:///{db_path}",
        incident_key="demo-incident",
        public_base_url="http://testserver",
        _env_file=None,
    )
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    clock = Clock()
    try:
        seeded = await seed_demo(session_factory, incident_key="demo-incident")
        async with session_factory() as session:
            incident_id = await session.scalar(
                sa.select(Incident.id).where(Incident.key == "demo-incident")
            )
        assert incident_id is not None

        intelligence = build_intelligence_provider(settings)
        inbox = build_inbox_provider(settings)
        mailbox = await inbox.provision_inbox("demo-incident")  # type: ignore[attr-defined]
        places = {
            place["name"]: (Decimal(place["lat"]), Decimal(place["lon"]))
            for place in json.loads(
                (ROOT / "fixtures/gazetteer.json").read_text(encoding="utf-8")
            )["places"]
        }
        handler = MessageJobHandler(
            session_factory=session_factory,
            inbox=inbox,  # type: ignore[arg-type]
            extraction=ExtractionService(incident_id, intelligence, FakeGeocoder(places)),
            notifications=NotificationService(),
            intelligence=intelligence,
            public_base_url="http://testserver",
            clock=clock,
        )
        worker = DurableWorker(
            session_factory,
            {JobType.PROCESS_MESSAGE: handler, JobType.FETCH_CONTENT: handler},
            clock=clock,
            jitter=lambda: 0.0,
            worker_id="replay-demo-worker",
        )
        ingestion = IngestionService(incident_id, session_factory, clock=clock)
        outbox = OutboxService(session_factory, inbox, clock=clock)  # type: ignore[arg-type]

        corpus = json.loads(
            (ROOT / "fixtures/messages.json").read_text(encoding="utf-8")
        )["messages"]
        durations: list[float] = []
        for item in corpus:
            provider_id = f"replay-{item['id'].lower()}"
            started = time.perf_counter()
            inbox.deliver_inbound(  # type: ignore[attr-defined]
                mailbox_id=mailbox.id,
                provider_message_id=provider_id,
                sender=item["sender"],
                subject=item["subject"],
                text=item["body"],
                thread_id=f"thread-{provider_id}",
                received_at=NOW,
            )
            await ingestion.accept(
                InboundEvent(
                    event_id=f"evt-{provider_id}",
                    event_type="message.received",
                    mailbox_id=mailbox.id,
                    provider_message_id=provider_id,
                    sender=item["sender"],
                    subject=item["subject"],
                    received_at=NOW,
                    withheld=False,
                    mailbox_address="incident@example.org",
                    thread_id=f"thread-{provider_id}",
                    raw=RawMessage(
                        provider_message_id=provider_id,
                        mailbox_id=mailbox.id,
                        sender=item["sender"],
                        subject=item["subject"],
                        text=item["body"],
                        received_at=NOW,
                        thread_id=f"thread-{provider_id}",
                    ),
                )
            )
            for _ in range(25):
                if await worker.run_once() == 0:
                    break
            while await outbox.deliver_due():
                pass
            durations.append((time.perf_counter() - started) * 1000)

        async with session_factory() as session:
            needs = int(
                await session.scalar(
                    sa.select(sa.func.count(Need.id)).where(
                        Need.incident_id == incident_id
                    )
                )
                or 0
            )

        container = Container(
            settings=settings, session_factory=session_factory, clock=clock, inbox=inbox  # type: ignore[arg-type]
        )
        app = create_app(settings, container)
        transport = httpx.ASGITransport(app=app)
        pages: dict[str, int] = {}
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            for name, url in (
                ("map", f"/api/v1/incidents/{incident_id}/map"),
                ("replay", f"/replay?incident_id={incident_id}"),
                ("lite", f"/replay/lite?incident_id={incident_id}"),
            ):
                pages[name] = (await client.get(url)).status_code
    finally:
        await engine.dispose()

    return {
        "mode": "replay",
        "seeded": seeded,
        "messages": len(durations),
        "needs_total": needs,
        "pipeline_ms_p50": round(median(durations), 1) if durations else None,
        "pages": pages,
        "external_connection_attempts": ATTEMPTS,
        "note": "Replay timing is the local deterministic pipeline's; it does "
        "not measure live sponsor latency.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay the demo corpus offline.")
    parser.add_argument("--report", default=None)
    args = parser.parse_args(argv)
    install_socket_guard()
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "replay.db"
        upgrade_database(db_path)
        report = asyncio.run(run(db_path))
    text = json.dumps(report, indent=2)
    print(text)
    if args.report:
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    ok = not ATTEMPTS and all(status == 200 for status in report["pages"].values())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

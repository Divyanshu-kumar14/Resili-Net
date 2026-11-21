"""Run the durable message and outbox loops for a provisioned incident.

This is the live worker entrypoint that the FastAPI process does not start on
its own. It requires a provisioned incident (``scripts/provision.py``) and the
same environment as the web service. SIGINT/SIGTERM stop the loop cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.adapters.nominatim import Gazetteer, NominatimGeocoder
from resilinet.composition import build_inbox_provider, build_intelligence_provider
from resilinet.config import Settings
from resilinet.db import create_engine, create_session_factory
from resilinet.models import Incident, JobType
from resilinet.observability import configure_logging
from resilinet.services.extraction import ExtractionService
from resilinet.services.notifications import NotificationService, OutboxService
from resilinet.workers.handlers import MessageJobHandler
from resilinet.workers.runner import DurableWorker

logger = logging.getLogger("resilinet.worker")

GAZETTEER_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "gazetteer.json"
POLL_SECONDS = 1.0
SCHEMA_WAIT_SECONDS = 60.0
INCIDENT_WAIT_SECONDS = 300.0
SCHEMA_RETRY_SECONDS = 2.0


async def _incident_id(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> UUID:
    started = asyncio.get_running_loop().time()
    schema_deadline = started + SCHEMA_WAIT_SECONDS
    incident_deadline = started + INCIDENT_WAIT_SECONDS
    while True:
        try:
            async with session_factory() as session:
                incident_id = await session.scalar(
                    sa.select(Incident.id).where(Incident.key == settings.incident_key)
                )
        except ProgrammingError:
            if asyncio.get_running_loop().time() >= schema_deadline:
                raise SystemExit(
                    "database schema is not ready; run `uv run alembic upgrade head` "
                    "before starting the worker"
                ) from None
            logger.info("worker_waiting_for_schema")
            await asyncio.sleep(SCHEMA_RETRY_SECONDS)
            continue
        if incident_id is not None:
            return incident_id
        if asyncio.get_running_loop().time() >= incident_deadline:
            raise SystemExit(
                f"incident {settings.incident_key!r} is not provisioned; "
                "run `uv run python scripts/provision.py`"
            )
        logger.info("worker_waiting_for_incident")
        await asyncio.sleep(SCHEMA_RETRY_SECONDS)


async def run(settings: Settings) -> None:
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    inbox = build_inbox_provider(settings)
    intelligence = build_intelligence_provider(settings)
    geocoder = NominatimGeocoder(
        session_factory=session_factory,
        gazetteer=Gazetteer.from_file(GAZETTEER_PATH),
        base_url=settings.nominatim_base_url,
        contact_email=settings.nominatim_contact_email,
        enabled=True,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_number, stop.set)

    try:
        incident_id = await _incident_id(settings, session_factory)
        handler = MessageJobHandler(
            session_factory=session_factory,
            inbox=inbox,
            extraction=ExtractionService(incident_id, intelligence, geocoder),
            notifications=NotificationService(),
            intelligence=intelligence,
            public_base_url=settings.public_base_url,
        )
        worker = DurableWorker(
            session_factory,
            {JobType.PROCESS_MESSAGE: handler, JobType.FETCH_CONTENT: handler},
        )
        outbox = OutboxService(session_factory, inbox)
        logger.info(
            "worker_started",
            extra={
                "incident_key": settings.incident_key,
                "provider": settings.llm_provider,
            },
        )
        while not stop.is_set():
            processed = await worker.run_once()
            delivered = await outbox.deliver_due()
            if not processed and not delivered:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
    finally:
        await geocoder.aclose()
        await inbox.aclose()
        close = getattr(intelligence, "aclose", None)
        if close is not None:
            await close()
        await engine.dispose()
        logger.info("worker_stopped")


def main() -> int:
    settings = Settings()
    if not settings.is_live:
        print("RESILINET_APP_ENV must be live to run the worker", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())

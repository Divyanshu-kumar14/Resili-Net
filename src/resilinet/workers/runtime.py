"""Shared durable worker runtime for the CLI and the in-app free-tier mode."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..adapters.nominatim import Gazetteer, NominatimGeocoder
from ..composition import build_auth_provider, build_intelligence_provider
from ..config import Settings
from ..models import Incident, JobType
from ..ports import InboxProvider, IntelligenceProvider
from ..services.extraction import ExtractionService
from ..services.identity import IdentityService
from ..services.notifications import NotificationService, OutboxService
from .handlers import MessageJobHandler
from .runner import DurableWorker

logger = logging.getLogger("resilinet.worker")

GAZETTEER_PATH = Path("fixtures/gazetteer.json")
POLL_SECONDS = 1.0
SCHEMA_WAIT_SECONDS = 60.0
INCIDENT_WAIT_SECONDS = 300.0
SCHEMA_RETRY_SECONDS = 2.0
RETRY_RUNTIME_SECONDS = 30.0


@dataclass(slots=True)
class WorkerRuntime:
    worker: DurableWorker
    outbox: OutboxService
    geocoder: NominatimGeocoder
    intelligence: IntelligenceProvider
    stop: asyncio.Event


async def resolve_incident_id(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> UUID:
    """Wait briefly for migrations and provisioning, then return the incident."""

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


async def build_worker_runtime(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    inbox: InboxProvider,
    stop: asyncio.Event,
) -> WorkerRuntime:
    incident_id = await resolve_incident_id(settings, session_factory)
    intelligence = build_intelligence_provider(settings)
    geocoder = NominatimGeocoder(
        session_factory=session_factory,
        gazetteer=Gazetteer.from_file(GAZETTEER_PATH),
        base_url=settings.nominatim_base_url,
        contact_email=settings.nominatim_contact_email,
        enabled=True,
    )
    handler = MessageJobHandler(
        session_factory=session_factory,
        inbox=inbox,
        extraction=ExtractionService(incident_id, intelligence, geocoder),
        notifications=NotificationService(),
        intelligence=intelligence,
        identity=IdentityService(session_factory, build_auth_provider(settings), settings),
        public_base_url=settings.public_base_url,
    )
    worker = DurableWorker(
        session_factory,
        {JobType.PROCESS_MESSAGE: handler, JobType.FETCH_CONTENT: handler},
    )
    outbox = OutboxService(session_factory, inbox)
    return WorkerRuntime(
        worker=worker,
        outbox=outbox,
        geocoder=geocoder,
        intelligence=intelligence,
        stop=stop,
    )


async def run_worker_loop(
    runtime: WorkerRuntime,
    *,
    on_heartbeat: Callable[[], None] | None = None,
) -> None:
    while not runtime.stop.is_set():
        if on_heartbeat is not None:
            on_heartbeat()
        processed = await runtime.worker.run_once()
        delivered = await runtime.outbox.deliver_due()
        if not processed and not delivered:
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    runtime.stop.wait(), timeout=POLL_SECONDS
                )


async def close_worker_runtime(runtime: WorkerRuntime) -> None:
    await runtime.geocoder.aclose()
    close = getattr(runtime.intelligence, "aclose", None)
    if close is not None:
        await close()


async def run_worker(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    inbox: InboxProvider,
    *,
    stop: asyncio.Event | None = None,
    on_heartbeat: Callable[[], None] | None = None,
) -> None:
    """Run the worker until ``stop`` is set.

    The CLI leaves ``stop`` unset so SIGINT/SIGTERM drive shutdown. The app
    passes its own event and cancels the task during lifespan shutdown.
    """

    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signal_number, stop.set)

    runtime = await build_worker_runtime(settings, session_factory, inbox, stop)
    try:
        logger.info(
            "worker_started",
            extra={
                "incident_key": settings.incident_key,
                "provider": settings.llm_provider,
            },
        )
        await run_worker_loop(runtime, on_heartbeat=on_heartbeat)
    finally:
        await close_worker_runtime(runtime)
        logger.info("worker_stopped")


__all__ = [
    "INCIDENT_WAIT_SECONDS",
    "RETRY_RUNTIME_SECONDS",
    "SCHEMA_RETRY_SECONDS",
    "SCHEMA_WAIT_SECONDS",
    "WorkerRuntime",
    "build_worker_runtime",
    "close_worker_runtime",
    "resolve_incident_id",
    "run_worker",
    "run_worker_loop",
]

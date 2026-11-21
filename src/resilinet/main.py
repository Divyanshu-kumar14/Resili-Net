"""Application composition and lifespan."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .api.routes import router
from .composition import build_inbox_provider
from .config import Settings
from .db import create_engine, create_session_factory
from .observability import (
    TraceIdMiddleware,
    configure_logging,
    install_access_log_redaction,
)
from .ports import InboxProvider
from .workers.runtime import RETRY_RUNTIME_SECONDS, run_worker

logger = logging.getLogger("resilinet.main")


@dataclass(frozen=True, slots=True)
class Container:
    """Dependencies shared by routes and background work.

    Tests and the replay mirror inject a container directly. A normal process
    builds one with :func:`build_container`; the inbound webhook stays disabled
    (503) when no session factory or inbox is configured, never silently fake.
    """

    settings: Settings
    session_factory: async_sessionmaker[AsyncSession] | None = None
    inbox: InboxProvider | None = None
    engine: AsyncEngine | None = None


def build_container(settings: Settings) -> Container:
    """Build the process container without performing any database IO."""
    engine = create_engine(settings)
    return Container(
        settings=settings,
        session_factory=create_session_factory(engine),
        inbox=build_inbox_provider(settings) if settings.is_live else None,
        engine=engine,
    )


async def _run_in_app_worker(app: FastAPI) -> None:
    """Run the durable worker in this process, retrying after startup failures."""

    container: Container = app.state.container
    if container.session_factory is None or container.inbox is None:
        logger.warning("in_app_worker_disabled")
        return
    stop = asyncio.Event()
    app.state.worker_stop = stop
    while not stop.is_set():
        try:
            await run_worker(
                container.settings,
                container.session_factory,
                container.inbox,
                stop=stop,
            )
            return
        except asyncio.CancelledError:
            raise
        except SystemExit as exc:
            logger.error("in_app_worker_stopped", extra={"reason": str(exc)})
        except Exception:
            logger.exception("in_app_worker_crashed")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=RETRY_RUNTIME_SECONDS)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Uvicorn resolves its logging configuration after importing the app, so
    # reinstall the access-log filter at startup as well.
    install_access_log_redaction()
    container: Container | None = getattr(app.state, "container", None)
    worker_task: asyncio.Task[None] | None = None
    if (
        container is not None
        and container.settings.is_live
        and container.settings.run_worker_in_app
    ):
        worker_task = asyncio.create_task(_run_in_app_worker(app))
    yield
    if worker_task is not None:
        stop = getattr(app.state, "worker_stop", None)
        if stop is not None:
            stop.set()
        worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await worker_task
    if container is None:
        return
    if container.inbox is not None:
        await container.inbox.aclose()
    if container.engine is not None:
        await container.engine.dispose()


def create_app(
    settings: Settings | None = None,
    container: Container | None = None,
) -> FastAPI:
    """Build the ASGI application.

    ``settings`` is validated before the application is returned, so a live
    configuration that is missing secrets fails at startup.
    """
    resolved = settings if settings is not None else Settings()
    configure_logging(resolved.log_level)
    app = FastAPI(
        title="ResiliNet",
        version="0.1.0",
        lifespan=_lifespan,
        docs_url=None if resolved.is_live else "/docs",
        redoc_url=None,
        openapi_url=None if resolved.is_live else "/openapi.json",
    )
    app.state.settings = resolved
    app.state.container = container if container is not None else build_container(resolved)
    app.add_middleware(TraceIdMiddleware)
    app.include_router(router)
    logger.info("application_started", extra={"mode": resolved.app_env})
    return app


def _default_app() -> Any:
    """ASGI factory used by ``uvicorn resilinet.main:app``."""
    return create_app()


app = _default_app()

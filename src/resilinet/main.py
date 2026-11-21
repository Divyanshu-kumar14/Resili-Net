"""Application composition and lifespan."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .adapters.agentboxd import AgentboxdInboxProvider
from .api.routes import router
from .config import Settings
from .db import create_engine, create_session_factory
from .observability import (
    TraceIdMiddleware,
    configure_logging,
    install_access_log_redaction,
)
from .ports import InboxProvider

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


def _live_inbox(settings: Settings) -> AgentboxdInboxProvider:
    api_key = settings.secret_value("agentboxd_api_key")
    webhook_secret = settings.secret_value("agentboxd_webhook_secret")
    if not api_key or not webhook_secret:
        raise RuntimeError("live mode reached inbox construction without Agentboxd secrets")
    return AgentboxdInboxProvider(
        api_key=api_key,
        webhook_secret=webhook_secret,
        base_url=settings.agentboxd_base_url,
        timeout_seconds=settings.request_timeout_seconds,
    )


def build_container(settings: Settings) -> Container:
    """Build the process container without performing any database IO."""
    engine = create_engine(settings)
    return Container(
        settings=settings,
        session_factory=create_session_factory(engine),
        inbox=_live_inbox(settings) if settings.is_live else None,
        engine=engine,
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Uvicorn resolves its logging configuration after importing the app, so
    # reinstall the access-log filter at startup as well.
    install_access_log_redaction()
    yield
    container: Container | None = getattr(app.state, "container", None)
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

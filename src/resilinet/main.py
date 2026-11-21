"""Application composition and lifespan."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from .api.routes import router
from .config import Settings
from .observability import (
    TraceIdMiddleware,
    configure_logging,
    install_access_log_redaction,
)

logger = logging.getLogger("resilinet.main")


@dataclass(frozen=True, slots=True)
class Container:
    """Dependencies shared by routes and background work.

    Phase 0 keeps the container intentionally small; later tasks add the
    adapters, pipeline, and durable worker here rather than in module globals.
    """

    settings: Settings


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Uvicorn resolves its logging configuration after importing the app, so
    # reinstall the access-log filter at startup as well.
    install_access_log_redaction()
    yield
    container: Container | None = getattr(app.state, "container", None)
    if container is not None:
        inbox = getattr(container, "inbox", None)
        if inbox is not None:
            await inbox.aclose()


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
    app.state.container = container if container is not None else Container(settings=resolved)
    app.add_middleware(TraceIdMiddleware)
    app.include_router(router)
    logger.info("application_started", extra={"mode": resolved.app_env})
    return app


def _default_app() -> Any:
    """ASGI factory used by ``uvicorn resilinet.main:app``."""
    return create_app()


app = _default_app()

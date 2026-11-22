"""Shared server-rendering and session helpers for the private web pages."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from ..security import SESSION_COOKIE_NAME, Principal
from ..services.identity import IdentityService

if TYPE_CHECKING:
    from ..main import Container

_TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


def container(request: Request) -> Container:
    return cast("Container", request.app.state.container)


def identity_service(request: Request) -> IdentityService:
    resolved = container(request)
    if resolved.session_factory is None:
        raise HTTPException(status_code=503, detail="identity is not configured")
    return IdentityService(
        resolved.session_factory,
        resolved.auth,
        resolved.settings,
        clock=resolved.clock,
    )


def session_cookie(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE_NAME)


async def current_principal(request: Request) -> Principal | None:
    return await identity_service(request).resolve_session(session_cookie(request))


async def require_principal(request: Request) -> Principal:
    principal = await current_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="sign in required")
    return principal


def error_page(
    request: Request,
    *,
    status_code: int,
    title: str,
    message: str,
) -> object:
    return templates.TemplateResponse(
        request,
        "notice.html",
        {"title": title, "message": message, "status_code": status_code},
        status_code=status_code,
    )


__all__ = [
    "container",
    "current_principal",
    "error_page",
    "identity_service",
    "require_principal",
    "session_cookie",
    "templates",
]

"""Secure participant action pages: one GET preview and one POST transition."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db import transaction
from ..security import private_headers
from ..services.actions import (
    ActionDenied,
    ActionError,
    ActionInvalid,
    ActionService,
    ActionStateError,
    ActionView,
)
from ..services.allocations import AllocationService
from ..services.notifications import NotificationService
from .common import (
    container,
    current_principal,
    identity_service,
    templates,
)

router = APIRouter()


def _action_service(request: Request) -> ActionService:
    resolved = container(request)
    if resolved.session_factory is None or resolved.auth is None:
        raise HTTPException(status_code=503, detail="actions are not configured")
    clock = resolved.clock
    return ActionService(
        AllocationService(clock=clock),
        NotificationService(),
        clock=clock,
    )


def _service_and_factory(
    request: Request,
) -> tuple[ActionService, async_sessionmaker[AsyncSession]]:
    resolved = container(request)
    if resolved.session_factory is None or resolved.auth is None:
        raise HTTPException(status_code=503, detail="actions are not configured")
    return _action_service(request), resolved.session_factory


def _notice(
    request: Request,
    *,
    status_code: int,
    title: str,
    message: str,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "notice.html",
        {"title": title, "message": message},
        status_code=status_code,
        headers=private_headers(),
    )


def _action_page(
    request: Request,
    *,
    token: str,
    view: ActionView,
    csrf_token: str,
    status_code: int = 200,
    error: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "action.html",
        {
            "token": token,
            "view": view,
            "csrf_token": csrf_token,
            "error": error,
        },
        status_code=status_code,
        headers=private_headers(),
    )


@router.get("/actions/{token}", response_class=HTMLResponse, tags=["actions"])
async def action_page(request: Request, token: str) -> HTMLResponse:
    """Render a safe form. A GET never changes state or consumes the grant."""
    principal = await current_principal(request)
    if principal is None:
        return _notice(
            request,
            status_code=401,
            title="Verify your email first",
            message=(
                "This private link only works for the verified address that "
                "received it. Open the verification message sent to you, then "
                "return to this link."
            ),
        )
    service, session_factory = _service_and_factory(request)
    try:
        async with session_factory() as session:
            grant = await service.load(session, token)
            if not service.is_live(grant):
                raise ActionInvalid("action link is no longer usable")
            await service.authorize(session, grant, principal)
            view = await service.view(session, grant)
    except ActionInvalid:
        return _notice(
            request,
            status_code=404,
            title="Link not available",
            message=(
                "This action link is invalid, expired, or already used. "
                "Request a new link by sending a new email."
            ),
        )
    except ActionDenied as exc:
        return _notice(
            request,
            status_code=403,
            title="Action not permitted",
            message=(
                f"{exc}. Ask an operator if you believe this is wrong."
            ),
        )
    return _action_page(
        request, token=token, view=view, csrf_token=principal.csrf_token
    )


@router.post("/actions/{token}", response_class=HTMLResponse, tags=["actions"])
async def action_submit(request: Request, token: str) -> HTMLResponse:
    """Consume a live grant and apply its single authorized transition."""
    principal = await current_principal(request)
    if principal is None:
        return _notice(
            request,
            status_code=401,
            title="Verify your email first",
            message=(
                "This private link only works for the verified address that "
                "received it. Open the verification message sent to you, then "
                "return to this link."
            ),
        )
    service, session_factory = _service_and_factory(request)
    form = await request.form()
    csrf = str(form.get("csrf_token", ""))
    if not await identity_service(request).verify_csrf(principal, csrf):
        return _notice(
            request,
            status_code=403,
            title="Request not verified",
            message=(
                "The form did not carry this session's security token. "
                "Reload the private link and submit it again."
            ),
        )

    try:
        async with transaction(session_factory) as session:
            grant = await service.load(session, token)
            if not service.is_live(grant):
                raise ActionInvalid("action link is no longer usable")
            result = await service.execute(
                session, grant_id=grant.id, principal=principal
            )
    except ActionInvalid:
        return _notice(
            request,
            status_code=404,
            title="Link not available",
            message=(
                "This action link is invalid, expired, or already used. "
                "Request a new link by sending a new email."
            ),
        )
    except ActionDenied as exc:
        return _notice(
            request,
            status_code=403,
            title="Action not permitted",
            message=f"{exc}. No change was made.",
        )
    except ActionStateError as exc:
        # The transaction rolled back; re-render the unchanged preview.
        try:
            async with session_factory() as session:
                grant = await service.load(session, token)
                view = await service.view(session, grant)
        except ActionError:
            return _notice(
                request,
                status_code=404,
                title="Link not available",
                message="This action link can no longer be used.",
            )
        return _action_page(
            request,
            token=token,
            view=view,
            csrf_token=principal.csrf_token,
            status_code=400,
            error=f"{exc}. No change was made.",
        )
    return _notice(
        request,
        status_code=200,
        title=result.title,
        message=result.message,
    )


__all__ = ["router"]

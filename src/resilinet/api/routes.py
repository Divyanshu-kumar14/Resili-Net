"""HTTP routes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from ..models import Incident
from ..ports import AdapterError, IgnoredWebhookEvent, WebhookVerificationError
from ..security import (
    AUTH_NONCE_COOKIE,
    SESSION_COOKIE_NAME,
    auth_nonce_cookie_header,
    clear_session_cookie_header,
    csrf_matches,
    new_opaque_token,
    private_headers,
    session_cookie_header,
    session_cookie_secure,
)
from ..services.identity import IdentityService
from ..services.ingestion import IngestionService
from ..web.common import current_principal, identity_service, templates

if TYPE_CHECKING:
    from ..main import Container

logger = logging.getLogger("resilinet.webhook")

router = APIRouter()


@router.get("/api/v1/health", tags=["system"])
async def health() -> dict[str, str]:
    """Liveness only: process is up and serving requests."""
    return {"status": "ok"}


@router.post(
    "/internal/agentboxd/events",
    status_code=204,
    response_class=Response,
    include_in_schema=False,
)
async def agentboxd_events(request: Request) -> Response:
    """Accept one signed Agentboxd envelope idempotently.

    The raw body is verified before it is parsed, a duplicate provider event is
    accepted without another job, and the response never reveals whether a
    message was new. The body is never logged.
    """
    container = cast("Container", request.app.state.container)
    if container.session_factory is None or container.inbox is None:
        raise HTTPException(status_code=503, detail="inbound webhook is not configured")

    raw = await request.body()
    try:
        event = await container.inbox.parse_webhook(raw, request.headers)
    except IgnoredWebhookEvent as exc:
        logger.info("ignored_agentboxd_event", extra={"event_type": exc.event_type})
        return Response(status_code=204)
    except WebhookVerificationError as exc:
        raise HTTPException(
            status_code=400, detail="invalid webhook signature or timestamp"
        ) from exc
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail="invalid webhook payload") from exc

    async with container.session_factory() as session:
        incident_id = await session.scalar(
            sa.select(Incident.id).where(Incident.key == container.settings.incident_key)
        )
    if incident_id is None:
        raise HTTPException(status_code=404, detail="incident is not provisioned")

    await IngestionService(incident_id, container.session_factory).accept(
        event, request.headers
    )
    return Response(status_code=204)


@router.get("/operator/login", response_class=HTMLResponse, tags=["identity"])
async def operator_login_page(request: Request) -> HTMLResponse:
    """Render the passwordless sign-in form; no state is changed."""
    return templates.TemplateResponse(
        request,
        "login.html",
        {},
        headers=private_headers(),
    )


@router.post("/operator/login", response_class=HTMLResponse, tags=["identity"])
async def operator_login(request: Request) -> HTMLResponse:
    """Start a throttled verification without revealing account existence."""
    form = await request.form()
    email = str(form.get("email", ""))[:320]
    service: IdentityService = identity_service(request)
    await service.start_operator_login(email)
    return templates.TemplateResponse(
        request,
        "notice.html",
        {
            "title": "Check your email",
            "message": (
                "If the address is registered, a sign-in verification message is "
                "on its way. Open it in this browser to finish signing in."
            ),
        },
        headers=private_headers(),
    )


@router.get("/auth/callback", response_class=HTMLResponse, tags=["identity"])
async def auth_callback(request: Request) -> HTMLResponse:
    """Render a confirmation step; a GET must not consume the link.

    Mailbox scanners and link previewers follow the provider's URL. Verifying
    only on POST keeps the state single-use for the human who clicks Continue.
    """
    service: IdentityService = identity_service(request)
    state_id = str(request.query_params.get("state_id", "")).strip()
    if not await service.pending_state_is_valid(state_id):
        return templates.TemplateResponse(
            request,
            "notice.html",
            {
                "title": "Sign-in link not accepted",
                "message": (
                    "This sign-in link is invalid, already used, or expired. "
                    "Request a new one from the sign-in page."
                ),
                "back_url": "/operator/login",
            },
            status_code=400,
            headers=private_headers(),
        )
    resolved = cast("Container", request.app.state.container)
    nonce = new_opaque_token()
    response = templates.TemplateResponse(
        request,
        "auth_callback.html",
        {"state_id": state_id, "nonce": nonce},
        headers=private_headers(),
    )
    response.headers["set-cookie"] = auth_nonce_cookie_header(
        nonce, secure=session_cookie_secure(resolved.settings)
    )
    return response


@router.post("/auth/callback", response_class=HTMLResponse, tags=["identity"])
async def auth_callback_submit(request: Request) -> Response:
    """Verify the provider callback and replace the URL with a local redirect."""
    service: IdentityService = identity_service(request)
    form = await request.form()
    state_id = str(form.get("state_id", "")).strip()
    nonce = str(form.get("nonce", ""))
    cookie_nonce = request.cookies.get(AUTH_NONCE_COOKIE)
    if not csrf_matches(cookie_nonce or "", nonce):
        return templates.TemplateResponse(
            request,
            "notice.html",
            {
                "title": "Sign-in confirmation expired",
                "message": (
                    "Open the sign-in link again from the same browser and "
                    "submit the confirmation page. Nothing was changed."
                ),
                "back_url": "/operator/login",
            },
            status_code=400,
            headers=private_headers(),
        )
    grant = await service.complete_callback({"state_id": state_id})
    if grant is None:
        return templates.TemplateResponse(
            request,
            "notice.html",
            {
                "title": "Sign-in link not accepted",
                "message": (
                    "This sign-in link is invalid, already used, or expired. "
                    "Request a new one from the sign-in page."
                ),
                "back_url": "/operator/login",
            },
            status_code=400,
            headers=private_headers(),
        )
    resolved = cast("Container", request.app.state.container)
    max_age = resolved.settings.session_ttl_minutes * 60
    response = RedirectResponse(url=grant.redirect_path, status_code=303)
    response.headers["set-cookie"] = session_cookie_header(
        grant.cookie_token,
        max_age_seconds=max_age,
        secure=session_cookie_secure(resolved.settings),
    )
    response.headers.update(private_headers())
    return response


@router.post("/auth/logout", tags=["identity"])
async def auth_logout(request: Request) -> Response:
    """Revoke the current session; requires its own CSRF token."""
    form = await request.form()
    service: IdentityService = identity_service(request)
    token = request.cookies.get(SESSION_COOKIE_NAME)
    csrf = str(form.get("csrf_token", ""))
    if not await service.logout(token, csrf):
        return templates.TemplateResponse(
            request,
            "notice.html",
            {
                "title": "Sign-out not accepted",
                "message": "Your session is missing, expired, or the request was not verified.",
                "back_url": "/operator/login",
            },
            status_code=403,
            headers=private_headers(),
        )
    resolved = cast("Container", request.app.state.container)
    response = RedirectResponse(url="/operator/login", status_code=303)
    response.headers["set-cookie"] = clear_session_cookie_header(
        secure=session_cookie_secure(resolved.settings)
    )
    response.headers.update(private_headers())
    return response


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home(request: Request) -> HTMLResponse:
    """A minimal landing page; private links are never listed here."""
    principal = await current_principal(request)
    return templates.TemplateResponse(
        request,
        "notice.html",
        {
            "title": "ResiliNet",
            "message": (
                "Email-native relief coordination. Participants act through the "
                "private single-use links sent to their verified address."
            ),
            "back_url": "/operator/login" if principal is None else "/operator/reviews",
        },
        headers=private_headers(),
    )

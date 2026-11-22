"""Minimal P0 operator review queue and resolution forms."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..db import transaction
from ..models import (
    Allocation,
    Message,
    Need,
    ParticipantRoleKind,
)
from ..security import Principal, private_headers
from ..services.actions import ActionIntakeService
from ..services.notifications import NotificationService
from ..services.reviews import (
    ReviewDenied,
    ReviewInvalid,
    ReviewService,
)
from .common import (
    container,
    identity_service,
    require_principal,
    templates,
)

router = APIRouter()


async def require_operator(request: Request) -> Principal:
    principal = await require_principal(request)
    identity = identity_service(request)
    if not await identity.has_role(principal, ParticipantRoleKind.OPERATOR):
        raise HTTPException(status_code=403, detail="operator role required")
    return principal


def _notice(
    request: Request,
    *,
    status_code: int,
    title: str,
    message: str,
    back_url: str = "/operator/reviews",
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "notice.html",
        {"title": title, "message": message, "back_url": back_url},
        status_code=status_code,
        headers=private_headers(),
    )


@router.get("/operator/reviews", response_class=HTMLResponse, tags=["operator"])
async def review_queue(request: Request) -> HTMLResponse:
    principal = await require_operator(request)
    resolved = container(request)
    if resolved.session_factory is None:
        raise HTTPException(status_code=503, detail="database is not configured")
    async with resolved.session_factory() as session:
        service = ReviewService(session, clock=resolved.clock)
        rows = await service.list_open(incident_id=principal.incident_id)
        need_refs: dict[object, str] = {}
        need_ids = {row.need_id for row in rows if row.need_id is not None}
        if need_ids:
            for need in await session.scalars(
                sa.select(Need).where(Need.id.in_(need_ids))
            ):
                need_refs[need.id] = need.ref
    cases = [
        SimpleNamespace(
            id=row.id,
            kind=row.kind,
            reason=row.reason,
            need_ref=need_refs.get(row.need_id),
            created_at=row.created_at,
        )
        for row in rows
    ]
    return templates.TemplateResponse(
        request,
        "reviews.html",
        {
            "principal": principal,
            "cases": cases,
            "csrf_token": principal.csrf_token,
        },
        headers=private_headers(),
    )


async def _detail_context(
    request: Request, principal: Principal, case_id: UUID, error: str | None = None
) -> dict[str, object]:
    resolved = container(request)
    if resolved.session_factory is None:
        raise HTTPException(status_code=503, detail="database is not configured")
    async with resolved.session_factory() as session:
        case = await ReviewService(session, clock=resolved.clock).get_case(
            incident_id=principal.incident_id, case_id=case_id
        )
        need = await session.get(Need, case.need_id) if case.need_id else None
        allocation = (
            await session.get(Allocation, case.allocation_id)
            if case.allocation_id
            else None
        )
        message = (
            await session.get(Message, case.source_message_id)
            if case.source_message_id
            else None
        )
        existing_need = None
        existing_id = case.payload.get("existing_need_id")
        if isinstance(existing_id, str):
            try:
                existing_need = await session.get(Need, UUID(existing_id))
            except ValueError:
                existing_need = None
    excerpt = None
    if message is not None:
        excerpt = (message.raw_text or message.body_text or "")[:2000]
    return {
        "principal": principal,
        "case": case,
        "need": need,
        "allocation": allocation,
        "excerpt": excerpt,
        "existing_need": existing_need,
        "csrf_token": principal.csrf_token,
        "error": error,
    }


@router.get(
    "/operator/reviews/{case_id}",
    response_class=HTMLResponse,
    tags=["operator"],
)
async def review_detail(request: Request, case_id: str) -> HTMLResponse:
    principal = await require_operator(request)
    try:
        parsed_id = UUID(case_id)
    except ValueError:
        return _notice(
            request,
            status_code=404,
            title="Review case not found",
            message="That review case does not exist in this incident.",
        )
    try:
        context = await _detail_context(request, principal, parsed_id)
    except ReviewInvalid:
        return _notice(
            request,
            status_code=404,
            title="Review case not found",
            message="That review case does not exist in this incident.",
        )
    return templates.TemplateResponse(
        request, "review_detail.html", context, headers=private_headers()
    )


@router.post("/operator/reviews/{case_id}/resolve", tags=["operator"])
async def resolve_review(request: Request, case_id: str) -> Response:
    principal = await require_operator(request)
    form = await request.form()
    csrf = str(form.get("csrf_token", ""))
    if not await identity_service(request).verify_csrf(principal, csrf):
        return _notice(
            request,
            status_code=403,
            title="Request not verified",
            message=(
                "The form did not carry this session's security token. "
                "Reload the review case and submit it again."
            ),
            back_url=f"/operator/reviews/{case_id}",
        )
    try:
        parsed_id = UUID(case_id)
    except ValueError:
        return _notice(
            request,
            status_code=404,
            title="Review case not found",
            message="That review case does not exist in this incident.",
        )
    resolution = str(form.get("resolution", "")).strip()
    quantity_raw = str(form.get("quantity", "")).strip()
    quantity: Decimal | None = None
    if quantity_raw:
        try:
            quantity = Decimal(quantity_raw)
        except InvalidOperation:
            return _notice(
                request,
                status_code=400,
                title="Quantity not accepted",
                message="Enter a finite quantity such as 120 or 120.5.",
                back_url=f"/operator/reviews/{case_id}",
            )
    resolved = container(request)
    if resolved.session_factory is None:
        raise HTTPException(status_code=503, detail="database is not configured")
    try:
        async with transaction(resolved.session_factory) as session:
            service = ReviewService(session, clock=resolved.clock)
            case = await service.resolve(
                case_id=parsed_id,
                incident_id=principal.incident_id,
                operator_id=principal.sender_id,
                resolution=resolution,
                quantity=quantity,
            )
            if resolution == "ASSIGN_DEPOT_COORDINATOR":
                pending_id = case.payload.get("pending_action_id")
                if isinstance(pending_id, str):
                    try:
                        pending_uuid = UUID(pending_id)
                    except ValueError:
                        pending_uuid = None
                    if pending_uuid is not None:
                        intake = ActionIntakeService(
                            NotificationService(),
                            public_base_url=resolved.settings.public_base_url,
                            clock=resolved.clock,
                        )
                        await intake.reissue_link(session, pending_uuid)
    except ReviewDenied:
        return _notice(
            request,
            status_code=403,
            title="Action not permitted",
            message="An incident operator role is required.",
        )
    except ReviewInvalid as exc:
        context = await _detail_context(
            request, principal, parsed_id, error=f"{exc}. No change was made."
        )
        return templates.TemplateResponse(
            request,
            "review_detail.html",
            context,
            status_code=400,
            headers=private_headers(),
        )
    return RedirectResponse(url="/operator/reviews", status_code=303)


__all__ = ["router"]

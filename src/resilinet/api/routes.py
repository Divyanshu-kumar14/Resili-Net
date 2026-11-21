"""HTTP routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request, Response

from ..models import Incident
from ..ports import AdapterError, WebhookVerificationError
from ..services.ingestion import IngestionService

if TYPE_CHECKING:
    from ..main import Container

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

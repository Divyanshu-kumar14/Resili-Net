"""Local-only offline demo mirror and its no-JavaScript equivalent.

These routes are a visual projection of the same public snapshot Momen binds.
They are deliberately unavailable in live mode: the hosted public application
owns the live map, and this mirror exists for a network-independent rehearsal.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db import utcnow
from ..models import Incident
from ..schemas import NeedStatus, Resource, Urgency
from ..security import public_headers
from ..services.public import (
    IncidentNotFound,
    InvalidPublicFilter,
    NotModified,
    PublicFilters,
    PublicProjectionError,
    PublicSnapshot,
)
from .common import container, public_service, templates

router = APIRouter()

_STATIC_DIR = Path(__file__).resolve().parents[3] / "static"
_SCHEMATIC_PATH = _STATIC_DIR / "schematic-map.svg"
_LABEL_LIMIT = 28

_SHAPES = {
    "shelter": "circle",
    "depot": "rect",
    "hub": "diamond",
    "other": "triangle",
}


@dataclass(frozen=True, slots=True)
class _Marker:
    entity_id: str
    name: str
    label: str
    kind: str
    shape: str
    x: float
    y: float
    href: str


_DANGEROUS_SVG = re.compile(
    r"<(script|foreignObject|iframe|image|use|a)\b[^>]*>.*?</\1\s*>"
    r"|<(script|foreignObject|iframe|image|use)\b[^>]*/>"
    r"|\s+on\w+\s*=\s*(\"[^\"]*\"|'[^']*')"
    r"|\s+(?:xlink:)?href\s*=\s*(\"[^\"]*\"|'[^']*')",
    re.DOTALL | re.IGNORECASE,
)


@lru_cache(maxsize=1)
def _schematic_background() -> str:
    """Inner SVG markup from the single local schematic asset.

    The file is trusted repository content, but it is rendered with ``|safe``,
    so active constructs (script, foreignObject, event handlers, links) are
    stripped as defence in depth and the result is cached after first read.
    """
    try:
        text = _SCHEMATIC_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"<svg\b[^>]*>(.*)</svg>\s*$", text, re.DOTALL)
    if not match:
        return ""
    return _DANGEROUS_SVG.sub("", match.group(1)).strip()


def _project(entities: Sequence[dict[str, Any]]) -> dict[str, tuple[float, float]]:
    """Linear schematic projection; never road distance or navigation."""
    placed = [
        entity
        for entity in entities
        if entity["lat"] is not None and entity["lon"] is not None
    ]
    if not placed:
        return {}
    lats = [float(entity["lat"]) for entity in placed]
    lons = [float(entity["lon"]) for entity in placed]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    lat_span = max_lat - min_lat
    lon_span = max_lon - min_lon
    positions: dict[str, tuple[float, float]] = {}
    for entity in placed:
        x = (
            50.0
            if lon_span == 0
            else 10.0 + 80.0 * ((float(entity["lon"]) - min_lon) / lon_span)
        )
        y = (
            30.0
            if lat_span == 0
            else 8.0 + 44.0 * ((max_lat - float(entity["lat"])) / lat_span)
        )
        positions[entity["id"]] = (round(x, 1), round(y, 1))
    return positions


def _markers(
    entities: Sequence[dict[str, Any]],
    needs: Sequence[dict[str, Any]],
    supplies: Sequence[dict[str, Any]],
) -> list[_Marker]:
    positions = _project(entities)
    first_need: dict[str, str] = {}
    for need in needs:
        first_need.setdefault(need["entity_id"], need["id"])
    first_supply: dict[str, str] = {}
    for supply in supplies:
        first_supply.setdefault(supply["entity_id"], supply["id"])

    markers: list[_Marker] = []
    for entity in entities:
        position = positions.get(entity["id"])
        if position is None:
            continue
        if entity["id"] in first_need:
            href = f"#need-{first_need[entity['id']]}"
        elif entity["id"] in first_supply:
            href = f"#supply-{first_supply[entity['id']]}"
        else:
            href = "#tables-heading"
        name = str(entity["name"])
        markers.append(
            _Marker(
                entity_id=str(entity["id"]),
                name=name,
                label=name if len(name) <= _LABEL_LIMIT else f"{name[: _LABEL_LIMIT - 1]}…",
                kind=str(entity["type"]),
                shape=_SHAPES.get(str(entity["type"]), "triangle"),
                x=position[0],
                y=position[1],
                href=href,
            )
        )
    return markers


def _freshness(generated_at: str, now: datetime) -> int:
    try:
        stamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(0, int((now - stamp).total_seconds()))


def _options() -> dict[str, list[dict[str, str]]]:
    def labelled(values: Sequence[str]) -> list[dict[str, str]]:
        return [
            {"value": value, "label": value.replace("_", " ").capitalize()}
            for value in values
        ]

    return {
        "resource": labelled([value.value for value in Resource]),
        "urgency": labelled([value.value for value in Urgency]),
        "status": labelled([value.value for value in NeedStatus]),
    }


def _query(filters: PublicFilters) -> dict[str, str]:
    values = {
        "resource": filters.resource.value if filters.resource else "",
        "urgency": filters.urgency.value if filters.urgency else "",
        "status": filters.status.value if filters.status else "",
    }
    return {key: value for key, value in values.items() if value}


async def _resolve_incident(
    session_factory: async_sessionmaker[AsyncSession],
    requested: str | None,
    incident_key: str,
) -> Incident:
    async with session_factory() as session:
        if requested:
            try:
                incident_id = UUID(requested)
            except ValueError as exc:
                raise IncidentNotFound("incident not found") from exc
            incident = await session.get(Incident, incident_id)
        else:
            incident = await session.scalar(
                sa.select(Incident).where(Incident.key == incident_key)
            )
    if incident is None:
        raise IncidentNotFound("incident not found")
    return incident


def _needs(rows: Sequence[dict[str, Any]], entities: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        entity = entities.get(row["entity_id"], {})
        flags: list[str] = []
        if not row["verified"]:
            flags.append("Unverified")
        if row["needs_human_review"]:
            flags.append("Human review")
        result.append(
            {
                **row,
                "entity_name": entity.get("name", "Unknown entity"),
                "entity_type": entity.get("type", "other"),
                "basis": row["quantity_source"],
                "basis_label": row["quantity_source"].capitalize(),
                "flags": flags,
            }
        )
    return result


def _supplies(
    rows: Sequence[dict[str, Any]], entities: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        {
            **row,
            "entity_name": entities.get(row["entity_id"], {}).get("name", "Unknown entity"),
            "entity_type": entities.get(row["entity_id"], {}).get("type", "other"),
        }
        for row in rows
    ]


def _allocations(
    rows: Sequence[dict[str, Any]],
    needs: dict[str, dict[str, Any]],
    entities: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        need = needs.get(row["need_id"], {})
        entity = entities.get(need.get("entity_id", ""), {})
        result.append(
            {
                **row,
                "entity_name": entity.get("name", "Unknown entity"),
            }
        )
    return result


def _snapshot_context(
    request: Request,
    incident: Incident,
    snapshot: PublicSnapshot,
    filters: PublicFilters,
) -> dict[str, Any]:
    body = snapshot.body
    entities = {entity["id"]: entity for entity in body["entities"]}
    needs = _needs(body["needs"], entities)
    supplies = _supplies(body["supplies"], entities)
    allocations = _allocations(
        body["allocations"],
        {need["id"]: need for need in body["needs"]},
        entities,
    )
    params = _query(filters)
    map_url = f"/api/v1/incidents/{incident.id}/map"
    if params:
        map_url = f"{map_url}?{urlencode(params)}"
    lite_url = f"/replay/lite?{urlencode({'incident_id': str(incident.id), **params})}"
    resolved = container(request)
    now = (resolved.clock or utcnow)()
    return {
        "incident_id": str(incident.id),
        "incident_name": incident.name,
        "simulation": bool(incident.simulation),
        "generated_at": body["generated_at"],
        "etag": snapshot.etag,
        "freshness_seconds": _freshness(body["generated_at"], now),
        "filters": {
            "resource": filters.resource.value if filters.resource else "",
            "urgency": filters.urgency.value if filters.urgency else "",
            "status": filters.status.value if filters.status else "",
        },
        "options": _options(),
        "markers": _markers(body["entities"], body["needs"], body["supplies"]),
        "needs": needs,
        "supplies": supplies,
        "allocations": allocations,
        "counts": {
            "entities": len(body["entities"]),
            "needs": len(needs),
            "supplies": len(supplies),
            "allocations": len(allocations),
        },
        "map_url": map_url,
        "lite_url": lite_url,
        "schematic_background": _schematic_background(),
    }


def _require_mirror(request: Request) -> None:
    if container(request).settings.is_live:
        raise HTTPException(status_code=404)


async def _view(
    request: Request,
    incident_id: str | None,
    resource: str | None,
    urgency: str | None,
    status: str | None,
) -> dict[str, Any]:
    _require_mirror(request)
    resolved = container(request)
    if resolved.session_factory is None:
        raise HTTPException(status_code=503, detail="public views are not configured")
    incident = await _resolve_incident(
        resolved.session_factory, incident_id, resolved.settings.incident_key
    )
    filters = PublicFilters.parse(resource=resource, urgency=urgency, status=status)
    snapshot = await public_service(request).map(incident.id, filters)
    if isinstance(snapshot, NotModified):
        raise HTTPException(status_code=500, detail="public view cache error")
    return _snapshot_context(request, incident, snapshot, filters)


@router.get("/replay", response_class=HTMLResponse, include_in_schema=False)
async def replay_page(
    request: Request,
    incident_id: str | None = None,
    resource: str | None = None,
    urgency: str | None = None,
    status: str | None = None,
) -> HTMLResponse:
    """Recorded/local mirror of the public map, table, and filters."""
    try:
        context = await _view(request, incident_id, resource, urgency, status)
    except InvalidPublicFilter:
        return templates.TemplateResponse(
            request,
            "notice.html",
            {
                "title": "Invalid filter",
                "message": "Invalid filter value. Choose one of the listed options.",
                "back_url": "/replay",
            },
            status_code=400,
            headers=public_headers(),
        )
    except IncidentNotFound:
        return templates.TemplateResponse(
            request,
            "notice.html",
            {
                "title": "Incident not found",
                "message": "Incident not found. Check the link and try again.",
                "back_url": "/replay",
            },
            status_code=404,
            headers=public_headers(),
        )
    except PublicProjectionError:
        return templates.TemplateResponse(
            request,
            "notice.html",
            {
                "title": "Public data unavailable",
                "message": "Public data is temporarily unavailable.",
                "back_url": "/replay",
            },
            status_code=503,
            headers=public_headers(),
        )
    return templates.TemplateResponse(
        request, "replay.html", context, headers=public_headers()
    )


@router.get("/replay/lite", response_class=HTMLResponse, include_in_schema=False)
async def replay_lite(
    request: Request,
    incident_id: str | None = None,
    resource: str | None = None,
    urgency: str | None = None,
    status: str | None = None,
) -> HTMLResponse:
    """No-JavaScript text equivalent.

    The seeded transfer (page plus local CSS) is verified under 100 KB in
    tests; a large incident renders proportionally more table rows, so the
    budget is a seeded-data property, not a per-incident guarantee.
    """
    try:
        context = await _view(request, incident_id, resource, urgency, status)
    except InvalidPublicFilter:
        return templates.TemplateResponse(
            request,
            "notice.html",
            {
                "title": "Invalid filter",
                "message": "Invalid filter value. Choose one of the listed options.",
                "back_url": "/replay/lite",
            },
            status_code=400,
            headers=public_headers(),
        )
    except IncidentNotFound:
        return templates.TemplateResponse(
            request,
            "notice.html",
            {
                "title": "Incident not found",
                "message": "Incident not found. Check the link and try again.",
                "back_url": "/replay/lite",
            },
            status_code=404,
            headers=public_headers(),
        )
    except PublicProjectionError:
        return templates.TemplateResponse(
            request,
            "notice.html",
            {
                "title": "Public data unavailable",
                "message": "Public data is temporarily unavailable.",
                "back_url": "/replay/lite",
            },
            status_code=503,
            headers=public_headers(),
        )
    return templates.TemplateResponse(
        request, "lite.html", context, headers=public_headers()
    )


__all__ = ["router"]

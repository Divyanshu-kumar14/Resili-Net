from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.adapters.nominatim import (
    Gazetteer,
    GazetteerError,
    NominatimGeocoder,
    normalize_place,
)
from resilinet.db import transaction
from resilinet.models import Allocation, Entity, GeocodeCache, Message, Need, Report, Sender, Supply
from resilinet.schemas import (
    EntityType,
    FieldConfidence,
    GeoConfidence,
    ParsedEmail,
    ParsedReport,
    ReportContext,
    ReportKind,
    Resource,
)
from resilinet.services.extraction import ReportPersistence

GAZETTEER_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "gazetteer.json"
NOW = datetime(2026, 11, 22, 9, 0, tzinfo=UTC)


class Tripwire:
    """An HTTP transport that fails if a test accidentally reaches the network."""

    def __init__(self) -> None:
        self.requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        raise AssertionError(f"unexpected network call to {request.url}")


class StubNominatim:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(200, json=outcome)


def http_for(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://nominatim.test",
    )


def geocoder(
    session_factory: async_sessionmaker[AsyncSession],
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    enabled: bool = True,
) -> NominatimGeocoder:
    return NominatimGeocoder(
        session_factory=session_factory,
        gazetteer=Gazetteer.from_file(GAZETTEER_PATH),
        base_url="https://nominatim.test",
        contact_email="ops@example.org",
        enabled=enabled,
        http=http_for(handler),
    )


def parsed_need(
    *,
    entity_name: str = "Shelter 4",
    location_text: str = "near Gandhi Park",
    lat: Decimal | None = Decimal("22.572600"),
    lon: Decimal | None = Decimal("88.363900"),
    confidence: GeoConfidence = GeoConfidence.EXACT,
) -> ParsedEmail:
    return ParsedEmail(
        context=ReportContext(
            kind=ReportKind.NEED,
            entity_name=entity_name,
            entity_type=EntityType.SHELTER,
            location_text=location_text,
            lat=lat,
            lon=lon,
            geo_confidence=confidence,
            confidence=FieldConfidence(location=0.95, items=0.95, headcount=0.9),
        ),
        reports=[ParsedReport(resource=Resource.POTABLE_WATER)],
    )


async def seed_message(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> tuple[UUID, UUID]:
    async with transaction(session_factory) as session:
        sender = Sender(incident_id=incident_id, address="volunteer@example.org")
        session.add(sender)
        await session.flush()
        message = Message(
            incident_id=incident_id,
            sender_id=sender.id,
            provider_message_id=f"msg-{uuid4()}",
            mailbox_id="mbx-1",
            subject="Shelter 4 water",
            body_text="Shelter 4 has 40 people and zero potable water.",
            raw_text="Shelter 4 has 40 people and zero potable water.",
            received_at=NOW,
        )
        session.add(message)
        await session.flush()
        return sender.id, message.id


def test_gazetteer_fixture_is_complete_and_simulation_labelled() -> None:
    import json

    payload = json.loads(GAZETTEER_PATH.read_text(encoding="utf-8"))
    assert payload["simulation"] is True
    assert len(payload["places"]) == 20
    gazetteer = Gazetteer.from_file(GAZETTEER_PATH)
    assert gazetteer.size >= 20


def test_gazetteer_rejects_out_of_range_coordinates() -> None:
    with pytest.raises(GazetteerError):
        Gazetteer({"Bad": (Decimal("91"), Decimal("0"))})


async def test_gazetteer_resolves_gandhi_park_without_network(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    tripwire = Tripwire()
    result = await geocoder(session_factory, tripwire).geocode(
        incident_id, "near Gandhi Park"
    )

    assert result.lat == Decimal("22.572600")
    assert result.lon == Decimal("88.363900")
    assert result.confidence is GeoConfidence.EXACT
    assert result.source == "gazetteer"
    assert tripwire.requests == 0


async def test_gazetteer_wins_over_a_poisoned_negative_cache(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    tripwire = Tripwire()
    async with transaction(session_factory) as session:
        session.add(
            GeocodeCache(
                incident_id=incident_id,
                normalized_location="gandhi park",
                lat=None,
                lon=None,
                confidence=GeoConfidence.NONE,
                source="nominatim",
            )
        )

    result = await geocoder(session_factory, tripwire).geocode(
        incident_id, "near Gandhi Park"
    )

    assert result.confidence is GeoConfidence.EXACT
    assert result.source == "gazetteer"
    assert tripwire.requests == 0


async def test_unicode_and_whitespace_normalize_the_cache_key(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    stub = StubNominatim([[{"lat": "22.600000", "lon": "88.400000"}]])
    client = geocoder(session_factory, stub)

    first = await client.geocode(incident_id, "  UnLiStEd\u00a0   Settlement ")
    second = await client.geocode(incident_id, "unlisted settlement")

    assert first == second
    assert len(stub.requests) == 1
    assert normalize_place("  GANDHI\u00a0   PaRk ") == "gandhi park"
    async with session_factory() as session:
        row = await session.scalar(sa.select(GeocodeCache))
    assert row is not None
    assert row.normalized_location == "unlisted settlement"


async def test_unknown_place_returns_none_without_network(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    tripwire = Tripwire()
    result = await geocoder(session_factory, tripwire, enabled=False).geocode(
        incident_id, "somewhere unlisted"
    )

    assert result.confidence is GeoConfidence.NONE
    assert tripwire.requests == 0
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count(GeocodeCache.id))) == 0


async def test_nominatim_result_is_area_and_cached(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    stub = StubNominatim([[{"lat": "22.600000", "lon": "88.400000"}]])
    client = geocoder(session_factory, stub)

    first = await client.geocode(incident_id, "unlisted settlement")
    second = await client.geocode(incident_id, "unlisted settlement")

    assert first.lat == Decimal("22.600000")
    assert first.confidence is GeoConfidence.AREA
    assert first.source == "nominatim"
    assert second == first
    assert len(stub.requests) == 1
    request = stub.requests[0]
    assert request.headers["User-Agent"].startswith("ResiliNet/0.1")
    assert "ops@example.org" in request.headers["User-Agent"]
    assert request.url.params["format"] == "jsonv2"


async def test_timeout_is_not_cached_and_can_be_retried(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    stub = StubNominatim(
        [
            httpx.ConnectTimeout("timeout"),
            [{"lat": "22.600000", "lon": "88.400000"}],
        ]
    )
    client = geocoder(session_factory, stub)

    first = await client.geocode(incident_id, "unlisted settlement")
    second = await client.geocode(incident_id, "unlisted settlement")

    assert first.confidence is GeoConfidence.NONE
    assert second.confidence is GeoConfidence.AREA
    assert len(stub.requests) == 2


async def test_malformed_response_is_not_cached(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    stub = StubNominatim(
        [
            {"not": "a list"},
            [{"lat": "22.600000", "lon": "88.400000"}],
        ]
    )
    client = geocoder(session_factory, stub)

    first = await client.geocode(incident_id, "unlisted settlement")
    second = await client.geocode(incident_id, "unlisted settlement")

    assert first.confidence is GeoConfidence.NONE
    assert second.confidence is GeoConfidence.AREA
    assert len(stub.requests) == 2


async def test_definitive_empty_result_is_negatively_cached(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    stub = StubNominatim([[]])
    client = geocoder(session_factory, stub)

    first = await client.geocode(incident_id, "unlisted settlement")
    second = await client.geocode(incident_id, "unlisted settlement")

    assert first.confidence is GeoConfidence.NONE
    assert second.confidence is GeoConfidence.NONE
    assert len(stub.requests) == 1
    async with session_factory() as session:
        row = await session.scalar(sa.select(GeocodeCache))
    assert row is not None
    assert row.confidence is GeoConfidence.NONE
    assert row.lat is None and row.lon is None


async def test_report_and_entity_are_persisted_without_coordinates(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    sender_id, message_id = await seed_message(session_factory, incident_id)
    parsed = parsed_need(
        lat=None, lon=None, confidence=GeoConfidence.NONE
    )

    async with transaction(session_factory) as session:
        result = await ReportPersistence(session).persist(
            incident_id=incident_id,
            message_id=message_id,
            sender_id=sender_id,
            parsed=parsed,
        )

    assert result.entity_id is not None
    async with session_factory() as session:
        entity = await session.get(Entity, result.entity_id)
        report = await session.get(Report, result.report_ids[0])
    assert entity is not None
    assert entity.lat is None and entity.lon is None
    assert entity.geo_confidence is GeoConfidence.NONE
    assert report is not None
    assert report.entity_id == entity.id
    assert report.lat is None and report.lon is None
    assert report.resource is Resource.POTABLE_WATER


async def test_a_later_report_with_coordinates_enriches_the_entity(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    sender_id, message_id = await seed_message(session_factory, incident_id)

    async with transaction(session_factory) as session:
        first = await ReportPersistence(session).persist(
            incident_id=incident_id,
            message_id=message_id,
            sender_id=sender_id,
            parsed=parsed_need(lat=None, lon=None, confidence=GeoConfidence.NONE),
        )
    async with transaction(session_factory) as session:
        second = await ReportPersistence(session).persist(
            incident_id=incident_id,
            message_id=message_id,
            sender_id=sender_id,
            parsed=parsed_need(entity_name="  shelter   4 "),
        )

    assert first.entity_id == second.entity_id
    async with session_factory() as session:
        entities = list(await session.scalars(sa.select(Entity)))
        entity = entities[0]
    assert len(entities) == 1
    assert entity.lat == Decimal("22.572600")
    assert entity.lon == Decimal("88.363900")


async def test_persistence_creates_no_stock_or_need(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> None:
    sender_id, message_id = await seed_message(session_factory, incident_id)

    async with transaction(session_factory) as session:
        await ReportPersistence(session).persist(
            incident_id=incident_id,
            message_id=message_id,
            sender_id=sender_id,
            parsed=parsed_need(),
        )

    async with session_factory() as session:
        for model in (Need, Supply, Allocation):
            count = await session.scalar(sa.select(sa.func.count()).select_from(model))
            assert count == 0

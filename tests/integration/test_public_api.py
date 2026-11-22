"""Task 17: versioned, privacy-safe public read API."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.config import Settings
from resilinet.db import transaction
from resilinet.domain.quantities import QuantitySource
from resilinet.main import Container, create_app
from resilinet.models import Allocation, Entity, EventLog, Incident, Need, Supply
from resilinet.schemas import (
    AllocationStatus,
    EntityType,
    NeedStatus,
    Resource,
    Urgency,
)

from .conftest import NOW, Clock

BASE_URL = "http://testserver"
FORBIDDEN_KEYS = {
    "address",
    "body_text",
    "email",
    "phone",
    "provider_message_id",
    "raw_text",
    "sender",
    "sender_id",
    "session",
    "token",
    "token_hash",
}


def make_settings(*, origins: tuple[str, ...] = ()) -> Settings:
    return Settings(
        app_env="test",
        incident_key="demo-incident",
        database_url="sqlite+aiosqlite:///:memory:",
        public_base_url=BASE_URL,
        momen_origins=origins,
        _env_file=None,
    )


def make_app(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    origins: tuple[str, ...] = (),
) -> FastAPI:
    settings = make_settings(origins=origins)
    container = Container(settings=settings, session_factory=session_factory, clock=clock)
    return create_app(settings, container)


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


async def seed_public_data(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
) -> dict[str, UUID]:
    """One shelter with coordinates, one with none, a depot, needs, supplies, allocations."""
    async with transaction(session_factory) as session:
        shelter = Entity(
            incident_id=incident_id,
            name="Shelter 4",
            normalized_name="shelter 4",
            entity_type=EntityType.SHELTER,
            location_text="Gandhi Park",
            lat=Decimal("18.520400"),
            lon=Decimal("73.856700"),
            geo_confidence="exact",
        )
        unplaced = Entity(
            incident_id=incident_id,
            name="Hill Camp",
            normalized_name="hill camp",
            entity_type=EntityType.SHELTER,
        )
        depot = Entity(
            incident_id=incident_id,
            name="Depot B",
            normalized_name="depot b",
            entity_type=EntityType.DEPOT,
            lat=Decimal("18.550000"),
            lon=Decimal("73.880000"),
            geo_confidence="area",
        )
        session.add_all([shelter, unplaced, depot])
        await session.flush()

        water_need = Need(
            incident_id=incident_id,
            entity_id=shelter.id,
            ref="RN-000001",
            resource=Resource.POTABLE_WATER,
            quantity=Decimal("120"),
            unit="l",
            quantity_source=QuantitySource.ESTIMATED,
            headcount=40,
            urgency=Urgency.CRITICAL,
            status=NeedStatus.MATCHED,
            verified=False,
        )
        food_need = Need(
            incident_id=incident_id,
            entity_id=unplaced.id,
            ref="RN-000002",
            resource=Resource.FOOD,
            quantity=Decimal("80"),
            unit="meal",
            quantity_source=QuantitySource.STATED,
            urgency=Urgency.NORMAL,
            status=NeedStatus.FULFILLED,
            verified=True,
            needs_human_review=True,
        )
        cancelled_need = Need(
            incident_id=incident_id,
            entity_id=shelter.id,
            ref="RN-000003",
            resource=Resource.POTABLE_WATER,
            quantity=Decimal("50"),
            unit="l",
            quantity_source=QuantitySource.STATED,
            urgency=Urgency.LOW,
            status=NeedStatus.CANCELLED,
        )
        session.add_all([water_need, food_need, cancelled_need])
        await session.flush()

        water_supply = Supply(
            incident_id=incident_id,
            entity_id=depot.id,
            resource=Resource.POTABLE_WATER,
            on_hand=Decimal("500"),
            unit="l",
        )
        food_supply = Supply(
            incident_id=incident_id,
            entity_id=depot.id,
            resource=Resource.FOOD,
            on_hand=Decimal("90"),
            unit="meal",
        )
        session.add_all([water_supply, food_supply])
        await session.flush()

        water_allocation = Allocation(
            incident_id=incident_id,
            need_id=water_need.id,
            supply_id=water_supply.id,
            ref="AL-000001",
            resource=Resource.POTABLE_WATER,
            quantity=Decimal("100"),
            unit="l",
            distance_km=Decimal("4.23"),
            status=AllocationStatus.PROPOSED,
            critic_verdict="approved",
        )
        food_allocation = Allocation(
            incident_id=incident_id,
            need_id=food_need.id,
            supply_id=food_supply.id,
            ref="AL-000002",
            resource=Resource.FOOD,
            quantity=Decimal("80"),
            unit="meal",
            delivered_quantity=Decimal("80"),
            distance_km=Decimal("7.0"),
            status=AllocationStatus.DELIVERED,
            critic_verdict="approved",
        )
        session.add_all([water_allocation, food_allocation])
        await session.flush()

        return {
            "shelter": shelter.id,
            "unplaced": unplaced.id,
            "depot": depot.id,
            "water_need": water_need.id,
            "food_need": food_need.id,
            "cancelled_need": cancelled_need.id,
            "water_supply": water_supply.id,
            "food_supply": food_supply.id,
            "water_allocation": water_allocation.id,
            "food_allocation": food_allocation.id,
        }


def walk(node: object) -> Iterator[tuple[str | None, object]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def assert_public_payload(payload: object) -> None:
    for key, value in walk(payload):
        if key is not None:
            assert key.casefold() not in FORBIDDEN_KEYS, key
        if isinstance(value, str):
            assert "@" not in value, value
            assert "token" not in value.casefold(), value


async def test_map_shape_labels_derived_stock_and_privacy(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    ids = await seed_public_data(session_factory, incident_id)
    app = make_app(session_factory, clock)

    async with client_for(app) as client:
        response = await client.get(f"/api/v1/incidents/{incident_id}/map")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "incident_id",
        "entities",
        "needs",
        "supplies",
        "allocations",
        "truncated",
        "generated_at",
    }
    assert payload["truncated"] == {
        "entities": False,
        "needs": False,
        "supplies": False,
        "allocations": False,
    }
    assert payload["incident_id"] == str(incident_id)
    assert payload["generated_at"] == "2026-11-22T09:00:00Z"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"].startswith('"')

    entities = {row["id"]: row for row in payload["entities"]}
    assert set(entities) == {str(ids["shelter"]), str(ids["unplaced"]), str(ids["depot"])}
    assert entities[str(ids["unplaced"])]["lat"] is None
    assert entities[str(ids["unplaced"])]["lon"] is None
    assert entities[str(ids["shelter"])]["type"] == "shelter"

    needs = {row["ref"]: row for row in payload["needs"]}
    assert set(needs) == {"RN-000001", "RN-000002", "RN-000003"}
    assert needs["RN-000001"]["quantity_source"] == "estimated"
    assert needs["RN-000001"]["quantity"] == "120.000"
    assert needs["RN-000001"]["urgency"] == "critical"
    assert needs["RN-000001"]["status"] == "matched"
    assert needs["RN-000001"]["verified"] is False
    assert needs["RN-000002"]["needs_human_review"] is True
    assert needs["RN-000002"]["quantity_source"] == "stated"

    supplies = {row["resource"]: row for row in payload["supplies"]}
    # 500 on hand minus the 100 l held by the proposed allocation.
    assert supplies["potable_water"]["available"] == "400.000"
    assert supplies["potable_water"]["on_hand"] == "500.000"
    assert supplies["food"]["available"] == "90.000"

    allocations = {row["ref"]: row for row in payload["allocations"]}
    assert allocations["AL-000001"]["need_ref"] == "RN-000001"
    assert allocations["AL-000001"]["need_id"] == str(ids["water_need"])
    assert allocations["AL-000001"]["supply_id"] == str(ids["water_supply"])
    # Approximate distance is deliberately coarse in public output.
    assert allocations["AL-000001"]["distance_km"] == "4.2"
    assert allocations["AL-000002"]["status"] == "delivered"

    assert_public_payload(payload)


async def test_map_filters_resource_urgency_and_status(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    await seed_public_data(session_factory, incident_id)
    app = make_app(session_factory, clock)
    base = f"/api/v1/incidents/{incident_id}/map"

    async with client_for(app) as client:
        food = await client.get(base, params={"resource": "food"})
        critical = await client.get(base, params={"urgency": "critical"})
        matched = await client.get(base, params={"status": "matched"})
        combined = await client.get(
            base, params={"resource": "potable_water", "status": "matched"}
        )
        invalid = await client.get(base, params={"resource": "gold"})
        invalid_status = await client.get(base, params={"status": "shipped"})
        missing = await client.get(f"/api/v1/incidents/{uuid4()}/map")
        malformed = await client.get("/api/v1/incidents/not-a-uuid/map")

    assert food.status_code == 200
    assert {row["resource"] for row in food.json()["needs"]} == {"food"}
    assert {row["resource"] for row in food.json()["supplies"]} == {"food"}
    assert {row["resource"] for row in food.json()["allocations"]} == {"food"}

    assert critical.status_code == 200
    assert [row["ref"] for row in critical.json()["needs"]] == ["RN-000001"]
    assert [row["ref"] for row in critical.json()["allocations"]] == ["AL-000001"]

    assert matched.status_code == 200
    assert [row["ref"] for row in matched.json()["needs"]] == ["RN-000001"]
    assert [row["ref"] for row in matched.json()["allocations"]] == ["AL-000001"]

    assert combined.status_code == 200
    assert [row["ref"] for row in combined.json()["needs"]] == ["RN-000001"]

    assert invalid.status_code == 400
    assert "gold" not in invalid.json()["detail"]
    assert invalid_status.status_code == 400
    assert missing.status_code == 404
    assert malformed.status_code == 422


async def test_etag_revalidation_and_revision_changes(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    ids = await seed_public_data(session_factory, incident_id)
    app = make_app(session_factory, clock)
    url = f"/api/v1/incidents/{incident_id}/map"

    async with client_for(app) as client:
        first = await client.get(url)
        etag = first.headers["etag"]

        fresh = await client.get(url)
        assert fresh.headers["etag"] == etag

        revalidated = await client.get(url, headers={"If-None-Match": etag})
        assert revalidated.status_code == 304
        assert revalidated.content == b""
        assert revalidated.headers["etag"] == etag

        star = await client.get(url, headers={"If-None-Match": "*"})
        assert star.status_code == 304

        filtered = await client.get(url, params={"resource": "food"})
        assert filtered.headers["etag"] != etag

    # A committed domain mutation bumps the public revision.
    async with transaction(session_factory) as session:
        need = await session.get(Need, ids["water_need"])
        assert need is not None
        need.quantity = Decimal("140")
        need.updated_at = NOW + timedelta(seconds=1)
        session.add(
            EventLog(
                incident_id=incident_id,
                event_type="need.revised",
                trace_id="trace-etag",
                actor_type="system",
                payload={"need_ref": "RN-000001"},
                created_at=NOW + timedelta(seconds=1),
            )
        )

    async with client_for(app) as client:
        changed = await client.get(url, headers={"If-None-Match": etag})
    assert changed.status_code == 200
    assert changed.headers["etag"] != etag


async def test_needs_endpoint_is_filtered_and_bounded(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    await seed_public_data(session_factory, incident_id)
    app = make_app(session_factory, clock)
    url = f"/api/v1/incidents/{incident_id}/needs"

    async with client_for(app) as client:
        response = await client.get(url, params={"status": "fulfilled"})
        invalid = await client.get(url, params={"resource": "unicorns"})
        unknown = await client.get(f"/api/v1/incidents/{uuid4()}/needs")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"incident_id", "needs", "truncated", "generated_at"}
    assert payload["truncated"] is False
    assert [row["ref"] for row in payload["needs"]] == ["RN-000002"]
    assert response.headers["etag"].startswith('"')
    assert_public_payload(payload)
    assert invalid.status_code == 400
    assert unknown.status_code == 404


async def test_summary_counts_and_decimal_totals(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    await seed_public_data(session_factory, incident_id)
    app = make_app(session_factory, clock)

    async with client_for(app) as client:
        response = await client.get(f"/api/v1/incidents/{incident_id}/summary")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"incident_id", "counts", "totals", "generated_at"}
    assert payload["counts"]["entities"] == 3
    assert payload["counts"]["needs"] == 3
    assert payload["counts"]["supplies"] == 2
    assert payload["counts"]["allocations"] == 2
    assert payload["counts"]["needs_by_status"] == {
        "matched": 1,
        "fulfilled": 1,
        "cancelled": 1,
    }
    assert payload["counts"]["needs_by_resource"] == {
        "potable_water": 2,
        "food": 1,
    }

    totals = payload["totals"]
    assert totals["requested"] == [
        {"resource": "food", "unit": "meal", "quantity": "80.000"},
        {"resource": "potable_water", "unit": "l", "quantity": "120.000"},
    ]
    assert totals["delivered"] == [
        {"resource": "food", "unit": "meal", "quantity": "80.000"},
        {"resource": "potable_water", "unit": "l", "quantity": "0.000"},
    ]
    assert totals["reserved"] == [
        {"resource": "food", "unit": "meal", "quantity": "0.000"},
        {"resource": "potable_water", "unit": "l", "quantity": "100.000"},
    ]
    assert totals["unfulfilled"] == [
        {"resource": "food", "unit": "meal", "quantity": "0.000"},
        {"resource": "potable_water", "unit": "l", "quantity": "120.000"},
    ]
    assert_public_payload(payload)


async def test_cors_allows_only_configured_origins_and_get(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    await seed_public_data(session_factory, incident_id)
    app = make_app(session_factory, clock, origins=("https://relief.example",))
    url = f"/api/v1/incidents/{incident_id}/map"

    async with client_for(app) as client:
        allowed = await client.get(url, headers={"Origin": "https://relief.example"})
        denied = await client.get(url, headers={"Origin": "https://attacker.example"})
        preflight = await client.options(
            url,
            headers={
                "Origin": "https://relief.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        get_preflight = await client.options(
            url,
            headers={
                "Origin": "https://relief.example",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert allowed.headers["access-control-allow-origin"] == "https://relief.example"
    assert "access-control-allow-credentials" not in allowed.headers
    assert "access-control-allow-origin" not in denied.headers
    # The browser rejects a failed preflight; POST is never granted.
    assert preflight.status_code == 400
    assert "POST" not in preflight.headers.get("access-control-allow-methods", "")
    assert get_preflight.status_code == 200
    assert (
        get_preflight.headers["access-control-allow-origin"] == "https://relief.example"
    )


async def test_unknown_incident_is_not_an_internal_error(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    app = make_app(session_factory, clock)
    async with client_for(app) as client:
        response = await client.get(f"/api/v1/incidents/{uuid4()}/summary")
    assert response.status_code == 404
    assert response.json() == {"detail": "incident not found"}


async def test_map_reports_only_incident_rows(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    await seed_public_data(session_factory, incident_id)
    async with transaction(session_factory) as session:
        other = Incident(key="other", name="Other", simulation=True)
        session.add(other)
        await session.flush()
        foreign = Entity(
            incident_id=other.id,
            name="Foreign Shelter",
            normalized_name="foreign shelter",
            entity_type=EntityType.SHELTER,
        )
        session.add(foreign)
        await session.flush()
        session.add(
            Need(
                incident_id=other.id,
                entity_id=foreign.id,
                ref="RN-900001",
                resource=Resource.FOOD,
                quantity=Decimal("10"),
                unit="meal",
                quantity_source=QuantitySource.STATED,
                status=NeedStatus.OPEN,
            )
        )

    app = make_app(session_factory, clock)
    async with client_for(app) as client:
        payload = (await client.get(f"/api/v1/incidents/{incident_id}/map")).json()

    assert "Foreign Shelter" not in {row["name"] for row in payload["entities"]}
    assert "RN-900001" not in {row["ref"] for row in payload["needs"]}
    assert await _allocation_count(session_factory) == 2


async def _allocation_count(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        return int(await session.scalar(sa.select(sa.func.count(Allocation.id))) or 0)


async def test_summary_totals_stay_exact_when_row_lists_truncate(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_public_data(session_factory, incident_id)
    monkeypatch.setattr("resilinet.services.public.PUBLIC_ROW_LIMIT", 2)
    app = make_app(session_factory, clock)

    async with client_for(app) as client:
        summary = await client.get(f"/api/v1/incidents/{incident_id}/summary")
        public_map = await client.get(f"/api/v1/incidents/{incident_id}/map")

    assert summary.status_code == 200
    counts = summary.json()["counts"]
    assert counts["entities"] == 3
    assert counts["needs"] == 3
    assert counts["supplies"] == 2
    assert counts["allocations"] == 2
    assert summary.json()["totals"]["requested"] == [
        {"resource": "food", "unit": "meal", "quantity": "80.000"},
        {"resource": "potable_water", "unit": "l", "quantity": "120.000"},
    ]

    assert public_map.status_code == 200
    payload = public_map.json()
    assert len(payload["needs"]) == 2
    assert payload["truncated"]["needs"] is True
    assert payload["truncated"]["entities"] is True


async def test_summary_keeps_delivered_history_of_cancelled_needs(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    ids = await seed_public_data(session_factory, incident_id)
    async with transaction(session_factory) as session:
        session.add(
            Allocation(
                incident_id=incident_id,
                need_id=ids["cancelled_need"],
                supply_id=ids["water_supply"],
                ref="AL-000003",
                resource=Resource.POTABLE_WATER,
                quantity=Decimal("50"),
                unit="l",
                distance_km=Decimal("4"),
                status=AllocationStatus.CANCELLED,
                delivered_quantity=Decimal("30"),
            )
        )
    app = make_app(session_factory, clock)

    async with client_for(app) as client:
        response = await client.get(f"/api/v1/incidents/{incident_id}/summary")

    assert response.status_code == 200
    totals = response.json()["totals"]
    water = {"resource": "potable_water", "unit": "l"}
    # 30 l were really delivered before the need was cancelled: history counts.
    assert {**water, "quantity": "30.000"} in totals["delivered"]
    # The cancelled need is not a request any more.
    assert {**water, "quantity": "120.000"} in totals["requested"]
    assert {**water, "quantity": "120.000"} in totals["unfulfilled"]


async def test_negative_availability_poisons_only_the_map(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    ids = await seed_public_data(session_factory, incident_id)
    async with transaction(session_factory) as session:
        supply = Supply(
            incident_id=incident_id,
            entity_id=ids["depot"],
            resource=Resource.MEDICINE,
            on_hand=Decimal("90"),
            unit="kit",
        )
        session.add(supply)
        await session.flush()
        session.add(
            Allocation(
                incident_id=incident_id,
                need_id=ids["food_need"],
                supply_id=supply.id,
                ref="AL-000004",
                resource=Resource.MEDICINE,
                quantity=Decimal("95"),
                unit="kit",
                distance_km=Decimal("4"),
                status=AllocationStatus.PROPOSED,
            )
        )
    app = make_app(session_factory, clock)

    async with client_for(app) as client:
        public_map = await client.get(f"/api/v1/incidents/{incident_id}/map")
        needs = await client.get(f"/api/v1/incidents/{incident_id}/needs")
        summary = await client.get(f"/api/v1/incidents/{incident_id}/summary")

    assert public_map.status_code == 500
    assert needs.status_code == 200
    assert summary.status_code == 200


async def test_etags_differ_between_representations(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    await seed_public_data(session_factory, incident_id)
    app = make_app(session_factory, clock)

    async with client_for(app) as client:
        etags = set()
        for kind in ("map", "needs", "summary"):
            response = await client.get(f"/api/v1/incidents/{incident_id}/{kind}")
            assert response.status_code == 200
            etags.add(response.headers["etag"])

    assert len(etags) == 3


async def test_cors_preflight_allows_the_etag_header(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    await seed_public_data(session_factory, incident_id)
    app = make_app(session_factory, clock, origins=("https://relief.example",))
    url = f"/api/v1/incidents/{incident_id}/map"

    async with client_for(app) as client:
        preflight = await client.options(
            url,
            headers={
                "Origin": "https://relief.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "if-none-match",
            },
        )
        response = await client.get(url, headers={"Origin": "https://relief.example"})

    assert preflight.status_code == 200
    assert "if-none-match" in preflight.headers["access-control-allow-headers"].casefold()
    assert "etag" in response.headers["access-control-expose-headers"].casefold()


def test_wildcard_momen_origin_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(momen_origins=("*",), _env_file=None)

"""Task 19: recorded Momen binding contract.

The editor spike, publish step, and measured reply-to-map latency remain an
unresolved account gate. These tests prove only that the recorded contract
matches the DTO the service actually produces and that it makes no live claim.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from fastapi.middleware.cors import CORSMiddleware

from resilinet.config import Settings
from resilinet.domain.quantities import QuantitySource
from resilinet.main import create_app
from resilinet.models import Allocation, Entity, Need, Supply
from resilinet.schemas import (
    AllocationStatus,
    EntityType,
    GeoConfidence,
    NeedStatus,
    Resource,
    Urgency,
)
from resilinet.services.public import map_snapshot

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "frontend/momen/api-contract.json").read_text(encoding="utf-8"))
FIXTURE = json.loads(
    (ROOT / "tests/fixtures/momen/map_snapshot.json").read_text(encoding="utf-8")
)

INCIDENT = UUID("11111111-1111-4111-8111-111111111111")
DEPOT = UUID("22222222-2222-4222-8222-222222222222")
SHELTER = UUID("33333333-3333-4333-8333-333333333333")
NEED = UUID("44444444-4444-4444-8444-444444444444")
SUPPLY = UUID("55555555-5555-4555-8555-555555555555")
CAMP = UUID("66666666-6666-4666-8666-666666666666")
FOOD_NEED = UUID("77777777-7777-4777-8777-777777777777")
STAMP = datetime(2026, 11, 22, 8, 50, tzinfo=UTC)


def live_projection() -> dict[str, object]:
    """The same rows as the recorded fixture, projected by the real DTO."""
    depot = Entity(
        id=DEPOT,
        incident_id=INCIDENT,
        name="Depot B",
        normalized_name="depot b",
        entity_type=EntityType.DEPOT,
        lat=Decimal("18.550000"),
        lon=Decimal("73.880000"),
        geo_confidence=GeoConfidence.AREA,
    )
    camp = Entity(
        id=CAMP,
        incident_id=INCIDENT,
        name="Hill Camp",
        normalized_name="hill camp",
        entity_type=EntityType.SHELTER,
        geo_confidence=GeoConfidence.NONE,
    )
    shelter = Entity(
        id=SHELTER,
        incident_id=INCIDENT,
        name="Shelter 4",
        normalized_name="shelter 4",
        entity_type=EntityType.SHELTER,
        lat=Decimal("18.520400"),
        lon=Decimal("73.856700"),
        geo_confidence=GeoConfidence.EXACT,
    )
    water = Need(
        id=NEED,
        incident_id=INCIDENT,
        entity_id=SHELTER,
        ref="RN-000001",
        resource=Resource.POTABLE_WATER,
        quantity=Decimal("120"),
        unit="l",
        quantity_source=QuantitySource.ESTIMATED,
        headcount=40,
        urgency=Urgency.CRITICAL,
        status=NeedStatus.MATCHED,
        verified=False,
        needs_human_review=False,
        created_at=STAMP,
        updated_at=STAMP,
    )
    food = Need(
        id=FOOD_NEED,
        incident_id=INCIDENT,
        entity_id=CAMP,
        ref="RN-000002",
        resource=Resource.FOOD,
        quantity=Decimal("80"),
        unit="meal",
        quantity_source=QuantitySource.STATED,
        urgency=Urgency.NORMAL,
        status=NeedStatus.OPEN,
        verified=True,
        needs_human_review=False,
        created_at=STAMP,
        updated_at=STAMP,
    )
    supply = Supply(
        id=SUPPLY,
        incident_id=INCIDENT,
        entity_id=DEPOT,
        resource=Resource.POTABLE_WATER,
        on_hand=Decimal("500"),
        unit="l",
        updated_at=STAMP,
    )
    allocation = Allocation(
        incident_id=INCIDENT,
        need_id=NEED,
        supply_id=SUPPLY,
        ref="AL-000001",
        resource=Resource.POTABLE_WATER,
        quantity=Decimal("120"),
        unit="l",
        distance_km=Decimal("4.23"),
        status=AllocationStatus.PROPOSED,
        critic_verdict="approved",
        updated_at=STAMP,
    )
    return map_snapshot(
        incident_id=INCIDENT,
        entities=[shelter, depot, camp],
        needs=[water, food],
        supplies=[supply],
        allocations=[allocation],
        available_by_supply={SUPPLY: Decimal("380")},
        generated_at=datetime(2026, 11, 22, 9, 0, tzinfo=UTC),
        truncated={
            "entities": False,
            "needs": False,
            "supplies": False,
            "allocations": False,
        },
    )


def _leaf_routes(routes: Iterable[object]) -> Iterator[object]:
    """Flatten included routers, which newer FastAPI keeps as wrappers."""
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _leaf_routes(original.routes)
        else:
            yield route


def app_routes() -> dict[str, object]:
    app = create_app(
        Settings(
            app_env="test",
            database_url="sqlite+aiosqlite:///:memory:",
            momen_origins=("https://relief.example",),
            _env_file=None,
        )
    )
    return {
        str(route.path): route
        for route in _leaf_routes(app.routes)
        if getattr(route, "path", None)
    }


def test_recorded_fixture_matches_the_live_projection() -> None:
    assert live_projection() == FIXTURE


def test_contract_fields_match_the_recorded_projection() -> None:
    assert set(FIXTURE) == set(CONTRACT["top_level_fields"])
    for name, spec in CONTRACT["collections"].items():
        rows = FIXTURE[name]
        assert rows, name
        assert {frozenset(row) for row in rows} == {frozenset(spec["fields"])}, name


def test_contract_endpoints_filters_and_operator_link_exist() -> None:
    routes = app_routes()
    for name, endpoint in CONTRACT["endpoints"].items():
        assert endpoint["method"] == "GET", name
        route = routes[endpoint["path"]]
        assert "GET" in (getattr(route, "methods", None) or set()), name
    map_route = routes[CONTRACT["endpoints"]["map"]["path"]]
    query_names = {
        param.name for param in map_route.dependant.query_params
    }
    assert query_names == set(CONTRACT["filters"])
    assert CONTRACT["operator_link"]["path"] in routes
    assert CONTRACT["operator_link"]["token_bearing"] is False


def test_cors_middleware_accepts_configured_momen_origins() -> None:
    app = create_app(
        Settings(
            app_env="test",
            database_url="sqlite+aiosqlite:///:memory:",
            momen_origins=("https://relief.example",),
            _env_file=None,
        )
    )
    cors = [
        middleware
        for middleware in app.user_middleware
        if middleware.cls is CORSMiddleware
    ]
    assert len(cors) == 1
    options = cors[0].kwargs
    assert options["allow_origins"] == ["https://relief.example"]
    assert options["allow_methods"] == ["GET"]
    assert options["allow_credentials"] is False
    assert options["allow_headers"] == ["If-None-Match"]
    assert options["expose_headers"] == ["ETag"]


def test_marker_and_list_parity_without_coordinates() -> None:
    entities = {entity["id"]: entity for entity in FIXTURE["entities"]}
    referenced = {
        row["entity_id"] for row in FIXTURE["needs"] + FIXTURE["supplies"]
    }
    assert referenced == set(entities)

    unplaced = [
        entity
        for entity in FIXTURE["entities"]
        if entity["lat"] is None or entity["lon"] is None
    ]
    assert unplaced, "the fixture must prove a no-coordinate entity stays list-visible"
    assert all(
        entity["geo_confidence"] == "none" for entity in unplaced
    )
    # List parity: the no-coordinate entity is still referenced by a need row,
    # which is how it remains visible without a marker.
    need_entity_ids = {row["entity_id"] for row in FIXTURE["needs"]}
    assert {entity["id"] for entity in unplaced} <= need_entity_ids


def test_no_action_token_session_or_provider_payload_is_bound() -> None:
    text = json.dumps(FIXTURE).casefold()
    for forbidden in CONTRACT["forbidden_fields"]:
        assert forbidden.casefold() not in text, forbidden
    assert "actions/" not in text
    assert "@" not in text


def test_refresh_contract_does_not_claim_an_unverified_latency() -> None:
    refresh = CONTRACT["refresh"]
    assert refresh["verified"] is False
    assert refresh["verified_interval_ms"] is None
    assert refresh["gate_status"] == "unresolved_account_gate"
    assert refresh["target_interval_ms"] == 1000
    assert refresh["fallback_interval_ms"] == 5000
    assert refresh["target_reply_to_map_ms"] == 5000
    assert "not a verified pass" in refresh["note"]


def test_lite_and_editor_checklist_are_declared() -> None:
    lite = CONTRACT["lite"]
    assert lite["max_bytes"] == 100000
    assert lite["javascript"] is False
    assert lite["map_tiles"] is False
    checklist = CONTRACT["editor_acceptance_checklist"]
    assert any("Simulation label" in item for item in checklist)
    assert any("aria-live" in item for item in checklist)
    assert any("No action token" in item for item in checklist)
    assert CONTRACT["page"]["simulation_label_required"] is True

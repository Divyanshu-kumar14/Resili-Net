"""Task 18: local-only replay mirror and equivalent no-JS lite view."""

from __future__ import annotations

import re
from uuid import UUID, uuid4

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.config import Settings
from resilinet.db import transaction
from resilinet.main import Container, create_app
from resilinet.models import Sender

from .conftest import Clock
from .test_public_api import BASE_URL, client_for, make_settings, seed_public_data


def make_replay_app(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> FastAPI:
    settings = make_settings()
    container = Container(settings=settings, session_factory=session_factory, clock=clock)
    return create_app(settings, container)


async def test_replay_page_has_landmarks_filters_markers_tables_and_freshness(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    await seed_public_data(session_factory, incident_id)
    app = make_replay_app(session_factory, clock)

    async with client_for(app) as client:
        response = await client.get("/replay", params={"incident_id": str(incident_id)})
        css = await client.get("/static/app.css")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text

    # Semantic landmarks and an explicit replay label.
    assert "<header" in html and "<nav" in html and "<main" in html
    assert "Replay" in html and "not live" in html.casefold()

    # Labelled filter controls, submitted as a normal GET form (works without JS).
    for field in ("resource", "urgency", "status"):
        assert f'<label for="{field}"' in html
        assert f'id="{field}"' in html
        assert f'name="{field}"' in html
    assert 'method="get"' in html
    assert 'action="/replay"' in html

    # Map markers use shape, label text, and an accessible name.
    assert "<svg" in html
    assert "<circle" in html  # shelter
    assert "<rect" in html  # depot
    assert "Shelter 4" in html and "Depot B" in html
    assert 'aria-label="Shelter 4' in html
    assert "<title>Shelter 4</title>" in html

    # Equivalent table toggle is visible, and tables are present without JS.
    assert "<summary>" in html
    assert "<table" in html
    assert "<caption>" in html
    assert "RN-000001" in html and "AL-000001" in html
    assert "estimated" in html and "stated" in html
    assert "unverified" in html.casefold()
    assert "review" in html.casefold()
    assert "not road" in html.casefold()

    # Freshness and polite announcements without stealing focus.
    assert "Updated 0 s ago" in html
    assert 'aria-live="polite"' in html
    assert "aria-atomic" in html

    # Local-only assets, reduced-motion rules, and a link to the lite view.
    assert 'src="/static/replay.js"' in html
    assert 'href="/static/app.css"' in html
    assert "/replay/lite" in html
    assert not re.search(r"https?://", html)
    assert css.status_code == 200
    assert "prefers-reduced-motion" in css.text
    assert "prefers-contrast" in css.text
    assert not re.search(r"url\(\s*['\"]?https?://", css.text)

    # Public pages must not use the token-page CSP (which forbids scripts).
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_replay_filters_reduce_the_same_rows(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    await seed_public_data(session_factory, incident_id)
    app = make_replay_app(session_factory, clock)

    async with client_for(app) as client:
        food = await client.get(
            "/replay",
            params={"incident_id": str(incident_id), "resource": "food"},
        )
        invalid = await client.get(
            "/replay",
            params={"incident_id": str(incident_id), "status": "shipped"},
        )

    assert food.status_code == 200
    assert "RN-000002" in food.text
    assert "RN-000001" not in food.text
    assert "AL-000002" in food.text
    assert "AL-000001" not in food.text

    assert invalid.status_code == 400
    assert "Traceback" not in invalid.text
    assert "invalid filter" in invalid.text.casefold()


async def test_lite_view_is_no_js_text_only_and_under_100kb(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    await seed_public_data(session_factory, incident_id)
    app = make_replay_app(session_factory, clock)

    async with client_for(app) as client:
        response = await client.get(
            "/replay/lite", params={"incident_id": str(incident_id)}
        )
        assets = []
        for match in re.findall(r'(?:href|src)="(/static/[^"]+)"', response.text):
            asset = await client.get(match)
            assert asset.status_code == 200
            assets.append(asset.content)

    assert response.status_code == 200
    html = response.text
    assert "<script" not in html.casefold()
    assert "<svg" not in html.casefold()
    assert "<table" in html
    assert "RN-000001" in html and "RN-000002" in html
    assert "estimated" in html and "stated" in html
    assert "Updated 0 s ago" in html
    assert 'href="/replay"' in html
    assert not re.search(r"https?://", html)
    transferred = len(response.content) + sum(len(asset) for asset in assets)
    assert transferred < 100_000, transferred


async def test_replay_does_not_render_private_participant_data(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    await seed_public_data(session_factory, incident_id)
    async with transaction(session_factory) as session:
        session.add(
            Sender(
                incident_id=incident_id,
                address="secret-sender@example.org",
                verified=True,
            )
        )
    app = make_replay_app(session_factory, clock)

    async with client_for(app) as client:
        page = await client.get("/replay", params={"incident_id": str(incident_id)})
        lite = await client.get("/replay/lite", params={"incident_id": str(incident_id)})

    for response in (page, lite):
        assert "secret-sender@example.org" not in response.text
        assert "example.org" not in response.text
        assert "token" not in response.text.casefold()
        assert "session" not in response.text.casefold()


async def test_replay_unknown_incident_is_a_404_page(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    app = make_replay_app(session_factory, clock)
    async with client_for(app) as client:
        response = await client.get("/replay", params={"incident_id": str(uuid4())})
    assert response.status_code == 404
    assert "not found" in response.text.casefold()
    assert "Traceback" not in response.text


async def test_replay_routes_are_absent_in_live_mode(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    live = Settings(
        app_env="live",
        incident_key="demo-incident",
        database_url="postgresql+asyncpg://user:pass@localhost/resilinet",
        public_base_url=BASE_URL,
        agentboxd_api_key="key",
        agentboxd_webhook_secret="secret",
        mojoauth_api_key="key",
        openai_api_key="key",
        _env_file=None,
    )
    container = Container(settings=live, session_factory=session_factory, clock=clock)
    app = create_app(live, container)

    async with client_for(app) as client:
        replay = await client.get("/replay")
        lite = await client.get("/replay/lite")

    assert replay.status_code == 404
    assert lite.status_code == 404


async def test_replay_defaults_to_the_configured_incident(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    await seed_public_data(session_factory, incident_id)
    app = make_replay_app(session_factory, clock)
    async with client_for(app) as client:
        response = await client.get("/replay")
    assert response.status_code == 200
    assert "RN-000001" in response.text


def test_public_headers_are_not_private_no_store_headers() -> None:
    from resilinet.security import private_headers, public_headers

    public = public_headers()
    assert public["Cache-Control"] == "no-cache"
    assert "script-src 'self'" in public["Content-Security-Policy"]
    assert private_headers()["Cache-Control"] == "no-store"

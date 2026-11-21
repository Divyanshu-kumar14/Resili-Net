from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from resilinet.db import create_session_factory, transaction
from resilinet.models import Incident

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 22, 9, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now = self.now + timedelta(**kwargs)


def upgrade_database(path: Path) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, "head")


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    path = tmp_path / "resilinet-test.db"
    upgrade_database(path)
    return path


@pytest.fixture
def database_url(database_path: Path) -> str:
    return f"sqlite+aiosqlite:///{database_path}"


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    from sqlalchemy.ext.asyncio import create_async_engine

    created = create_async_engine(database_url)
    yield created
    await created.dispose()


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
async def incident_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> UUID:
    async with transaction(session_factory) as session:
        incident = Incident(key="demo-incident", name="Demo Incident", simulation=True)
        session.add(incident)
        await session.flush()
        return incident.id


@pytest.fixture
def clock() -> Clock:
    return Clock()

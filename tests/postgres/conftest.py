"""Task 22 scratch-PostgreSQL fixtures.

A uniquely named scratch database is created on a disposable local
PostgreSQL (docker-compose.test.yml), migrated, and dropped afterwards.
The name is verified before any destructive cleanup. This suite NEVER
touches a developer or production database: the admin URL defaults to
the local test container and can be overridden with
RESILINET_TEST_PG_ADMIN_URL. When PostgreSQL is unreachable the suite
skips instead of failing.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.config import Settings
from resilinet.db import create_engine, create_session_factory

ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL = os.environ.get(
    "RESILINET_TEST_PG_ADMIN_URL",
    "postgresql+asyncpg://resilinet_test:resilinet_test@localhost:54329/postgres",
)
PREFIX = "resilinet_scratch_"


def _admin_dsn() -> str:
    return ADMIN_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _create_database(name: str) -> None:
    conn = await asyncpg.connect(_admin_dsn())
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _drop_database(name: str) -> None:
    assert name.startswith(PREFIX), f"refusing to drop non-scratch database {name!r}"
    conn = await asyncpg.connect(_admin_dsn())
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            name,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def scratch_url() -> str:
    name = f"{PREFIX}{secrets.token_hex(6)}"
    try:
        asyncio.run(_create_database(name))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(
            "local test PostgreSQL unavailable "
            f"({type(exc).__name__}); start it with: "
            "docker compose -f docker-compose.test.yml up -d"
        )
    url = _admin_dsn().rsplit("/", 1)[0] + f"/{name}"
    env = {
        **os.environ,
        "RESILINET_DATABASE_URL": url.replace("postgresql://", "postgresql+asyncpg://", 1),
        "RESILINET_APP_ENV": "test",
    }
    alembic_bin = Path(sys.executable).parent / "alembic"
    result = subprocess.run(
        [str(alembic_bin), "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:  # pragma: no cover - migration failure is a real failure
        asyncio.run(_drop_database(name))
        raise RuntimeError(f"scratch migration failed:\n{result.stdout}\n{result.stderr}")
    yield url.replace("postgresql://", "postgresql+asyncpg://", 1)
    asyncio.run(_drop_database(name))


@pytest.fixture
async def pg_factory(scratch_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings(
        app_env="test", database_url=scratch_url, _env_file=None
    )
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()

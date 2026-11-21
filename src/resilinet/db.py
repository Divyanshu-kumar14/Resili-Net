"""Async engine, session factory, transaction helper, and UTC timestamp type."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.types import DateTime, TypeDecorator

from .config import Settings


def utcnow() -> datetime:
    """Return an aware UTC timestamp for column defaults and services."""
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Store UTC and always return an aware UTC ``datetime``.

    SQLite drops timezone information, while PostgreSQL keeps it. Round-tripping
    through this type keeps domain comparisons identical on both backends.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def create_engine(settings: Settings) -> AsyncEngine:
    """Create the process engine for the configured database URL."""
    return create_async_engine(settings.database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Run a block in one transaction, committing on success and rolling back on error."""
    async with session_factory() as session, session.begin():
        yield session

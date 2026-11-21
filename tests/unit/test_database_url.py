from __future__ import annotations

import pytest

from resilinet.db import normalize_async_database_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "postgres://user:pass@host:5432/resilinet",
            "postgresql+asyncpg://user:pass@host:5432/resilinet",
        ),
        (
            "postgresql://user:pass@host:5432/resilinet",
            "postgresql+asyncpg://user:pass@host:5432/resilinet",
        ),
        (
            "postgresql+asyncpg://user:pass@host:5432/resilinet",
            "postgresql+asyncpg://user:pass@host:5432/resilinet",
        ),
        ("sqlite+aiosqlite:///./resilinet.db", "sqlite+aiosqlite:///./resilinet.db"),
        (
            "  postgresql://user:pass@host/resilinet  ",
            "postgresql+asyncpg://user:pass@host/resilinet",
        ),
    ],
)
def test_bare_postgres_urls_are_routed_through_asyncpg(
    raw: str, expected: str
) -> None:
    assert normalize_async_database_url(raw) == expected

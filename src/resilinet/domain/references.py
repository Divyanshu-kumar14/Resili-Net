"""Public reference codes.

``RN-`` identifies a Need; ``AL-`` identifies one Allocation. Codes are
six-digit, zero-padded, unique, and drawn from a durable counter that starts at
a random offset so the sequence is not enumerable from the first public email.

The counter row is locked for the duration of the creating transaction, so two
workers on PostgreSQL cannot mint the same code. A rolled-back transaction
returns its number instead of burning it, which a bare database sequence would
not do.
"""

from __future__ import annotations

import re
import secrets
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ReferenceCounter

ReferenceKind = Literal["RN", "AL"]

REFERENCE_PATTERN = re.compile(r"^(RN|AL)-([0-9]{6})$")
#: Durable allocators start high enough that the code is not trivially guessable.
REFERENCE_MIN = 100_000
REFERENCE_MAX = 999_999
#: The Phase 0 in-memory sequence still starts at 1; the public format allows it.
REFERENCE_FLOOR = 1


class ReferenceError(ValueError):
    """A reference code is malformed, exhausted, or cannot be allocated."""


class ReferenceSequence:
    """Monotonic in-memory allocator used by the Phase 0 slice.

    It starts at a random six-digit offset by default, so the slice cannot emit
    an enumerable ``RN-000001`` even before it is replaced by the durable path.
    """

    def __init__(self, start: int | None = None) -> None:
        value = start if start is not None else _random_start()
        if value < 1:
            raise ValueError("start must be at least 1")
        self._next: dict[ReferenceKind, int] = {"RN": value, "AL": value}

    def next(self, kind: ReferenceKind) -> str:
        value = self._next[kind]
        self._next[kind] = value + 1
        return format_reference(kind, value)


def format_reference(kind: ReferenceKind, value: int) -> str:
    """Render and validate one six-digit reference code."""
    if not REFERENCE_FLOOR <= value <= REFERENCE_MAX:
        raise ReferenceError(f"reference value {value} is outside the public range")
    return f"{kind}-{value:06d}"


def parse_reference(value: str) -> tuple[ReferenceKind, int]:
    """Return ``(kind, number)`` for a valid public code, else raise."""
    match = REFERENCE_PATTERN.fullmatch(value.strip().upper())
    if match is None:
        raise ReferenceError(f"{value!r} is not a public reference code")
    kind: ReferenceKind = "RN" if match.group(1) == "RN" else "AL"
    return kind, int(match.group(2))


def _random_start() -> int:
    return secrets.randbelow(REFERENCE_MAX - REFERENCE_MIN + 1) + REFERENCE_MIN


async def new_reference(
    session: AsyncSession,
    kind: ReferenceKind,
    *,
    start_offset: int | None = None,
) -> str:
    """Allocate the next code from the durable counter inside ``session``.

    ``start_offset`` exists for deterministic tests and seed scripts. Uniqueness
    is still enforced by the ``needs.ref``/``allocations.ref`` constraints; a
    caller that loses a race retries the insert with a fresh code.
    """
    if start_offset is not None and not REFERENCE_MIN <= start_offset <= REFERENCE_MAX:
        raise ReferenceError("start_offset must be a six-digit public code")
    counter = await _locked_counter(session, kind)
    if counter is None:
        start = start_offset if start_offset is not None else _random_start()
        try:
            async with session.begin_nested():
                session.add(ReferenceCounter(kind=kind, next_value=start))
                await session.flush()
        except IntegrityError:
            # Another transaction created the counter between our select and
            # insert; re-read the committed row below.
            pass
        counter = await _locked_counter(session, kind)
        if counter is None:
            raise ReferenceError(f"could not initialise the {kind} counter")
    value = counter.next_value
    if value > REFERENCE_MAX:
        raise ReferenceError(f"the {kind} reference space is exhausted")
    counter.next_value = value + 1
    await session.flush()
    return format_reference(kind, value)


async def _locked_counter(
    session: AsyncSession, kind: ReferenceKind
) -> ReferenceCounter | None:
    statement = (
        sa.select(ReferenceCounter)
        .where(ReferenceCounter.kind == kind)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


__all__ = [
    "REFERENCE_FLOOR",
    "REFERENCE_MAX",
    "REFERENCE_MIN",
    "REFERENCE_PATTERN",
    "ReferenceError",
    "ReferenceKind",
    "ReferenceSequence",
    "format_reference",
    "new_reference",
    "parse_reference",
]

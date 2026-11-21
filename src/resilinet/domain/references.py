"""Public reference codes.

``RN-`` identifies a Need; ``AL-`` identifies one Allocation. Codes are
six-digit, zero-padded, and unique within the incident.
"""

from __future__ import annotations

from typing import Literal

ReferenceKind = Literal["RN", "AL"]


class ReferenceSequence:
    """Monotonic code allocator.

    Phase 0 keeps the counter in memory. The durable implementation replaces the
    backing store with a database sequence and a random starting offset while
    preserving the same public format.
    """

    def __init__(self, start: int = 1) -> None:
        if start < 1:
            raise ValueError("start must be at least 1")
        self._next: dict[ReferenceKind, int] = {"RN": start, "AL": start}

    def next(self, kind: ReferenceKind) -> str:
        value = self._next[kind]
        self._next[kind] = value + 1
        return f"{kind}-{value:06d}"

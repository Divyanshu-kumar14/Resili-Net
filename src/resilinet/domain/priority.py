"""Deterministic need scheduling priority.

Priority orders *scheduling only*; it never changes quantities or stock.
Verified senders win ties, they do not gain priority. Medical escalations
receive the explicitly retained ``+20`` scheduling bonus while still
requiring human review before any stock is committed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from ..schemas import Urgency

URGENCY_WEIGHTS: dict[Urgency, int] = {
    Urgency.CRITICAL: 4,
    Urgency.HIGH: 3,
    Urgency.NORMAL: 2,
    Urgency.LOW: 1,
    Urgency.UNKNOWN: 0,
}
HEADCOUNT_CAP = 200
MEDICAL_BONUS = Decimal("20")
_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class NeedPriorityView:
    id: UUID
    urgency: Urgency
    headcount: int | None
    created_at: datetime
    verified: bool
    is_medical: bool


def priority_score(need: NeedPriorityView, now: datetime) -> Decimal:
    """``urgency*10 + min(headcount,200)/10 + age_hours*0.5 + medical``."""
    age_seconds = Decimal(str(max(0.0, (now - need.created_at).total_seconds())))
    age_hours = age_seconds / Decimal("3600")
    headcount = min(need.headcount or 0, HEADCOUNT_CAP)
    score = (
        Decimal(URGENCY_WEIGHTS[need.urgency] * 10)
        + Decimal(headcount) / Decimal("10")
        + age_hours * Decimal("0.5")
        + (MEDICAL_BONUS if need.is_medical else Decimal("0"))
    )
    return score.quantize(_QUANTUM)


def rank_needs(
    needs: list[NeedPriorityView], now: datetime
) -> list[NeedPriorityView]:
    """Order by priority DESC, verified DESC, created_at ASC, id ASC."""
    return sorted(
        needs,
        key=lambda need: (
            -priority_score(need, now),
            not need.verified,
            need.created_at,
            str(need.id),
        ),
    )

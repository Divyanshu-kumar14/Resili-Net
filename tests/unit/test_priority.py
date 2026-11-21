from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from resilinet.domain.priority import NeedPriorityView, priority_score, rank_needs
from resilinet.schemas import Urgency

NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)


def view(
    suffix: int,
    *,
    urgency: Urgency = Urgency.NORMAL,
    headcount: int | None = None,
    age_hours: float = 0,
    verified: bool = False,
    is_medical: bool = False,
) -> NeedPriorityView:
    return NeedPriorityView(
        id=UUID(int=suffix),
        urgency=urgency,
        headcount=headcount,
        created_at=NOW - timedelta(hours=age_hours),
        verified=verified,
        is_medical=is_medical,
    )


def test_priority_formula_combines_urgency_headcount_and_age() -> None:
    # critical (4*10) + 40 people (4) + 10 h old (5) = 49
    score = priority_score(view(1, urgency=Urgency.CRITICAL, headcount=40, age_hours=10), NOW)

    assert score == Decimal("49.0000")


def test_headcount_contribution_is_capped_at_two_hundred() -> None:
    capped = priority_score(view(1, headcount=500), NOW)
    at_cap = priority_score(view(2, headcount=200), NOW)

    assert capped == at_cap == Decimal("40.0000")  # normal 20 + 200/10


def test_medical_bonus_is_exactly_twenty() -> None:
    plain = priority_score(view(1), NOW)
    medical = priority_score(view(2, is_medical=True), NOW)

    assert medical - plain == Decimal("20")


def test_unknown_urgency_has_zero_weight() -> None:
    assert priority_score(view(1, urgency=Urgency.UNKNOWN), NOW) == Decimal("0.0000")


def test_verified_is_only_a_tie_break_not_a_priority_boost() -> None:
    urgent_unverified = view(1, urgency=Urgency.CRITICAL)
    calm_verified = view(2, urgency=Urgency.LOW, verified=True)

    ranked = rank_needs([calm_verified, urgent_unverified], NOW)

    assert ranked[0].id == urgent_unverified.id


def test_equal_priority_falls_back_to_verified_then_oldest_then_id() -> None:
    older = view(9, age_hours=2)
    newer_verified = view(3, age_hours=2, verified=True)
    # Same age/urgency/headcount, so priority is identical for all three.
    same_age_low_id = view(1, age_hours=2)
    same_age_high_id = view(2, age_hours=2)

    ranked = rank_needs([same_age_high_id, older, newer_verified, same_age_low_id], NOW)

    assert ranked[0].id == newer_verified.id  # verified wins the tie
    assert [item.id for item in ranked[1:]] == [
        same_age_low_id.id,
        same_age_high_id.id,
        older.id,
    ]


def test_ranking_is_deterministic_for_equal_inputs() -> None:
    needs = [view(3), view(1), view(2)]

    first = rank_needs(needs, NOW)
    second = rank_needs(list(reversed(needs)), NOW)

    assert [item.id for item in first] == [item.id for item in second]

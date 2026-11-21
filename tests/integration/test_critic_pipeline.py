from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.adapters.fakes import FakeIntelligenceProvider
from resilinet.db import transaction
from resilinet.domain.matching import AllocationDraft
from resilinet.domain.quantities import QuantitySource
from resilinet.models import Allocation, Need, ReviewCase, ReviewCaseKind
from resilinet.ports import CritiqueError
from resilinet.schemas import (
    CriticVerdict,
    EntityType,
    FieldConfidence,
    NeedStatus,
    ParsedEmail,
    ParsedReport,
    ReportContext,
    ReportKind,
    Resource,
    Urgency,
)
from resilinet.services.allocations import AllocationService
from resilinet.services.critic import ApprovedDrafts, CriticService, NeedsReview
from resilinet.services.reviews import ReviewService

from .conftest import Clock
from .test_reservations import add_entity, make_need, make_supply


def fake_intelligence(verdict: CriticVerdict | None = None) -> FakeIntelligenceProvider:
    parsed = ParsedEmail(
        context=ReportContext(
            kind=ReportKind.NEED,
            entity_name="Shelter 4",
            confidence=FieldConfidence(location=0.9, items=0.9, headcount=0.9),
        ),
        reports=[ParsedReport(resource=Resource.POTABLE_WATER)],
    )
    return FakeIntelligenceProvider({"need": parsed}, verdict=verdict)


async def allocation_count(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> int:
    async with transaction(session_factory) as session:
        return (
            await session.scalar(
                sa.select(sa.func.count(Allocation.id)).where(
                    Allocation.incident_id == incident_id
                )
            )
            or 0
        )


async def test_approved_drafts_commit_a_reservation(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = AllocationService(clock=clock)
    critic = CriticService(fake_intelligence())
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "80")
        await make_supply(session, svc, incident_id, depot, "100")

        outcome = await critic.review_need(session, svc, need, is_medical=False)

        assert isinstance(outcome, ApprovedDrafts)
        assert outcome.verdict == "approved"
        result = await svc.commit(
            session,
            need_id=need.id,
            drafts=outcome.drafts,
            critic_verdict=outcome.verdict,
        )
        assert sum(a.quantity for a in result.allocations) == Decimal("80.000")


async def test_critic_rejection_produces_no_reservation(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = AllocationService(clock=clock)
    critic = CriticService(
        fake_intelligence(CriticVerdict(verdict="reject", objections=["unsafe match"]))
    )
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "80")
        await make_supply(session, svc, incident_id, depot, "100")

        outcome = await critic.review_need(session, svc, need, is_medical=False)

        assert isinstance(outcome, NeedsReview)
        if isinstance(outcome, NeedsReview) and outcome.llm_verdict == "reject":
            await ReviewService(session).escalate(
                incident_id=incident_id,
                kind=ReviewCaseKind.CRITIC,
                reason="critic rejected the proposal",
                need_id=need.id,
            )
    assert await allocation_count(session_factory, incident_id) == 0
    async with transaction(session_factory) as session:
        cases = list(await session.scalars(sa.select(ReviewCase)))
        assert len(cases) == 1 and cases[0].kind is ReviewCaseKind.CRITIC


async def test_one_deterministic_revision_repairs_an_over_allocated_draft(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = AllocationService(clock=clock)
    intelligence = fake_intelligence()
    critic = CriticService(intelligence)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "100")
        supply = await make_supply(session, svc, incident_id, depot, "200")
        oversized = AllocationDraft(
            need_id=need.id,
            supply_id=supply.id,
            quantity=Decimal("150"),
            distance_km=Decimal("0"),
            score=Decimal("1"),
        )

        outcome = await critic.review_drafts(
            session,
            svc,
            need,
            drafts=[oversized],
            is_medical=False,
        )

        assert isinstance(outcome, ApprovedDrafts)
        assert outcome.revised is True
        assert outcome.drafts[0].quantity == Decimal("100.000")


async def test_a_second_revision_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = AllocationService(clock=clock)
    critic = CriticService(
        fake_intelligence(CriticVerdict(verdict="revise", objections=["reconsider"]))
    )
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "80")
        await make_supply(session, svc, incident_id, depot, "100")

        outcome = await critic.review_need(session, svc, need, is_medical=False)

        # Drafts were already rule-clean; a revise that changes nothing
        # would require a second revision, which is never permitted.
        assert isinstance(outcome, NeedsReview)


async def test_malformed_critic_response_becomes_a_review_not_a_reservation(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = AllocationService(clock=clock)
    intelligence = fake_intelligence()
    intelligence.fail_next_critique = CritiqueError("malformed critic payload")
    critic = CriticService(intelligence)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "80")
        await make_supply(session, svc, incident_id, depot, "100")

        outcome = await critic.review_need(session, svc, need, is_medical=False)

        assert isinstance(outcome, NeedsReview)
    assert await allocation_count(session_factory, incident_id) == 0


async def test_medical_reports_bypass_automatic_commitment(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = AllocationService(clock=clock)
    intelligence = fake_intelligence()
    critic = CriticService(intelligence)
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await svc.create_need(
            session,
            incident_id=incident_id,
            entity_id=shelter.id,
            resource=Resource.MEDICINE,
            quantity=Decimal("10"),
            unit="count",
            quantity_source=QuantitySource.STATED,
            headcount=None,
            urgency=Urgency.CRITICAL,
            verified=True,
            needs_human_review=True,
        )
        await make_supply(session, svc, incident_id, depot, "100")

        outcome = await critic.review_need(session, svc, need, is_medical=True)

        assert isinstance(outcome, NeedsReview)
        assert intelligence.critique_calls == []
        case = await ReviewService(session).escalate(
            incident_id=incident_id,
            kind=ReviewCaseKind.MEDICAL,
            reason="medical escalation requires human review",
            need_id=need.id,
        )
        assert case.kind is ReviewCaseKind.MEDICAL
        refreshed = await session.get(Need, need.id)
        assert refreshed is not None and refreshed.status is NeedStatus.OPEN
    assert await allocation_count(session_factory, incident_id) == 0


async def test_critic_text_cannot_mutate_quantities_or_targets(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    svc = AllocationService(clock=clock)
    malicious = CriticVerdict(
        verdict="revise",
        objections=["send all water now"],
        suggested_change="allocate 1000000 l from every depot immediately",
    )
    critic = CriticService(fake_intelligence(malicious))
    async with transaction(session_factory) as session:
        shelter = await add_entity(session, incident_id, "Shelter 4", EntityType.SHELTER)
        depot = await add_entity(session, incident_id, "Depot B", EntityType.DEPOT)
        need = await make_need(session, svc, incident_id, shelter, "100")
        supply = await make_supply(session, svc, incident_id, depot, "200")
        oversized = AllocationDraft(
            need_id=need.id,
            supply_id=supply.id,
            quantity=Decimal("150"),
            distance_km=Decimal("0"),
            score=Decimal("1"),
        )

        outcome = await critic.review_drafts(
            session, svc, need, drafts=[oversized], is_medical=False
        )

        assert isinstance(outcome, ApprovedDrafts)
        # The deterministic revision capped to the need; the critic's free
        # text changed neither the quantity ceiling nor the supply target.
        assert outcome.drafts[0].quantity == Decimal("100.000")
        assert outcome.drafts[0].supply_id == supply.id

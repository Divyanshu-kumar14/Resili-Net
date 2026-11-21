"""Bounded adversarial critique of allocation drafts.

Deterministic rules run first and last. The LLM critic is advisory: it may
approve, reject, or request exactly one revision, and the only revision
ever applied is the deterministic repair in ``domain.rules`` — critic free
text can never change IDs, resources, units, or quantity ceilings.
Medical escalations bypass the critic (and any automatic commitment)
entirely and go to human review.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from ..db import utcnow
from ..domain.matching import AllocationDraft, SupplyMatchView
from ..domain.rules import (
    DraftValidationContext,
    RuleViolation,
    SupplyValidationView,
    revise_drafts,
    validate_drafts,
)
from ..models import Entity, Need
from ..ports import CritiqueError, IntelligenceProvider
from ..schemas import CriticCandidate, CriticContext, GeoConfidence
from .allocations import AllocationService

_MEDICAL_REASON = (
    "medical escalation requires human review before any stock is committed"
)


@dataclass(frozen=True, slots=True)
class ApprovedDrafts:
    drafts: tuple[AllocationDraft, ...]
    verdict: str  # "approved" | "approved_after_revision"
    revised: bool


@dataclass(frozen=True, slots=True)
class NeedsReview:
    reason: str
    violations: tuple[RuleViolation, ...] = ()
    llm_verdict: str | None = None


class CriticService:
    def __init__(self, intelligence: IntelligenceProvider) -> None:
        self._intelligence = intelligence

    async def review_need(
        self,
        session: AsyncSession,
        allocations: AllocationService,
        need: Need,
        *,
        is_medical: bool,
        now: datetime | None = None,
    ) -> ApprovedDrafts | NeedsReview:
        """Draft, then review, the current best proposal for one need."""
        drafts = await allocations.drafts_for_need(session, need.id, now or utcnow())
        return await self.review_drafts(
            session, allocations, need, drafts=drafts, is_medical=is_medical
        )

    async def prepare(
        self,
        session: AsyncSession,
        allocations: AllocationService,
        need: Need,
        *,
        drafts: list[AllocationDraft],
    ) -> tuple[DraftValidationContext, CriticContext]:
        """Build rule/critic contexts inside the caller's read session.

        Splitting preparation from :meth:`review` lets callers close the
        database session before the LLM call, so no transaction or
        connection is ever held across provider latency.
        """
        supply_views = await allocations.supply_views(session, need)
        validation = await self._validation_context(
            session, allocations, need, supply_views
        )
        context = await self._critic_context(session, need, drafts, supply_views)
        return validation, context

    async def review_drafts(
        self,
        session: AsyncSession,
        allocations: AllocationService,
        need: Need,
        *,
        drafts: list[AllocationDraft],
        is_medical: bool,
    ) -> ApprovedDrafts | NeedsReview:
        """Review explicit drafts (used when drafts were built externally)."""
        if is_medical:
            return NeedsReview(reason=_MEDICAL_REASON)
        validation, context = await self.prepare(
            session, allocations, need, drafts=drafts
        )
        return await self.review(
            validation=validation, context=context, drafts=drafts
        )

    async def review(
        self,
        *,
        validation: DraftValidationContext,
        context: CriticContext,
        drafts: list[AllocationDraft],
    ) -> ApprovedDrafts | NeedsReview:
        """Apply rules → one advisory critique → at most one revision."""
        if context.is_medical:
            return NeedsReview(reason=_MEDICAL_REASON)
        violations = validate_drafts(drafts, validation)
        try:
            verdict = await self._intelligence.critique(context)
        except CritiqueError as exc:
            return NeedsReview(
                reason=f"critic response could not be used: {exc}",
                violations=tuple(violations),
            )
        if verdict.verdict == "reject":
            objections = "; ".join(verdict.objections) or "no reason given"
            return NeedsReview(
                reason=f"critic rejected the proposal: {objections}",
                violations=tuple(violations),
                llm_verdict="reject",
            )
        if not violations and verdict.verdict == "approve":
            return ApprovedDrafts(
                drafts=tuple(drafts), verdict="approved", revised=False
            )
        if not drafts:
            return ApprovedDrafts(drafts=(), verdict="approved", revised=False)

        # Exactly one bounded revision: deterministic repair, then re-check.
        revised = revise_drafts(drafts, validation)
        if not revised:
            return NeedsReview(
                reason="no valid candidate remains after revision",
                violations=tuple(violations),
                llm_verdict=verdict.verdict,
            )
        remaining_violations = validate_drafts(revised, validation)
        if remaining_violations:
            return NeedsReview(
                reason="revision still violates deterministic rules",
                violations=tuple(remaining_violations),
                llm_verdict=verdict.verdict,
            )
        if revised == drafts:
            return NeedsReview(
                reason="critic requested a revision that changes nothing; "
                "a second revision is not permitted",
                llm_verdict=verdict.verdict,
            )
        return ApprovedDrafts(
            drafts=tuple(revised), verdict="approved_after_revision", revised=True
        )

    # -- context builders ----------------------------------------------------

    async def _validation_context(
        self,
        session: AsyncSession,
        allocations: AllocationService,
        need: Need,
        supply_views: list[SupplyMatchView],
    ) -> DraftValidationContext:
        entity = await session.get(Entity, need.entity_id)
        remaining = await allocations.remaining_for_need(session, need)
        return DraftValidationContext(
            need_id=need.id,
            resource=need.resource.value,
            unit=need.unit,
            remaining=remaining,
            need_lat=entity.lat if entity is not None else None,
            need_lon=entity.lon if entity is not None else None,
            need_geo_confidence=(
                entity.geo_confidence if entity is not None else GeoConfidence.NONE
            ),
            supplies={
                view.id: SupplyValidationView(
                    id=view.id,
                    resource=view.resource,
                    unit=view.unit,
                    available=view.available,
                    entity_lat=view.entity_lat,
                    entity_lon=view.entity_lon,
                )
                for view in supply_views
            },
        )

    async def _critic_context(
        self,
        session: AsyncSession,
        need: Need,
        drafts: list[AllocationDraft],
        supply_views: list[SupplyMatchView],
    ) -> CriticContext:
        entity = await session.get(Entity, need.entity_id)
        by_id = {view.id: view for view in supply_views}
        now = utcnow()
        candidates: list[CriticCandidate] = []
        for index, draft in enumerate(drafts, start=1):
            view = by_id.get(draft.supply_id)
            age_hours = Decimal("0")
            if view is not None:
                seconds = max(0.0, (now - view.updated_at).total_seconds())
                age_hours = (Decimal(str(seconds)) / Decimal("3600")).quantize(
                    Decimal("0.01")
                )
            candidates.append(
                CriticCandidate(
                    allocation_ref=f"{need.ref}:draft-{index}",
                    need_ref=need.ref,
                    supply_ref=str(draft.supply_id),
                    resource=need.resource,
                    quantity=draft.quantity,
                    unit=need.unit,
                    distance_km=draft.distance_km,
                    available_quantity=(
                        view.available if view is not None else Decimal("0")
                    ),
                    freshness_hours=age_hours,
                )
            )
        return CriticContext(
            incident_id=need.incident_id,
            need_ref=need.ref,
            entity_name=entity.name if entity is not None else None,
            location_text=entity.location_text if entity is not None else None,
            urgency=need.urgency,
            is_medical=need.needs_human_review,
            candidates=candidates,
        )

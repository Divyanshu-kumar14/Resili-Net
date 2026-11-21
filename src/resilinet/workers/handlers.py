"""Durable handlers for inbound message jobs.

Each handler follows the same boundary: read in one short transaction, perform
model/HTTP work outside any transaction, then write the outcome in one
transaction that re-checks the message state under a row lock. A job retried
after a commit therefore becomes a no-op instead of a duplicate report.

Phase 2 matching follows the same boundary: reports are registered
transactionally, drafts and the bounded critic run outside any transaction,
and the reservation commit — with replies, action grants, and review cases —
commits atomically. If a crash separates registration from matching, the
commit phase is rebuilt on retry (see ``_recovery_work``); commits recompute
remaining quantities and every notification is guarded by its outbox kind,
so recovery can never double-reserve or double-send.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db import utcnow
from ..domain.lifecycle import ACTIVE_ALLOCATION_STATUSES
from ..domain.matching import AllocationDraft
from ..domain.quantities import (
    CanonicalUnit,
    Quantity,
    QuantitySource,
    format_quantity,
)
from ..domain.rules import DraftValidationContext, is_medical_report
from ..models import (
    ActionGrant,
    Allocation,
    Entity,
    EventLog,
    Job,
    JobStatus,
    JobType,
    Message,
    MessageStatus,
    Need,
    OutboundEmail,
    ParticipantRole,
    ParticipantRoleKind,
    PendingActionKind,
    Report,
    ReviewCaseKind,
    Sender,
    Supply,
)
from ..ports import (
    InboxProvider,
    IntelligenceProvider,
    MessageNotFoundError,
    ProviderUnavailableError,
    RawMessage,
)
from ..schemas import (
    ClarificationRequired,
    CriticContext,
    NeedStatus,
    ParsedEmail,
    ReportKind,
)
from ..services.actions import create_action_grant
from ..services.allocations import AllocationService, RegistrationResult
from ..services.clarification import (
    SPLIT_FIELD,
    ClarificationOutcome,
    ClarificationService,
)
from ..services.critic import ApprovedDrafts, CriticService, NeedsReview
from ..services.events import record_event
from ..services.extraction import ExtractionService, InjectionFinding
from ..services.ingestion import normalize_inbound_text
from ..services.notifications import (
    NotificationService,
    OutboxService,
    RecommendationText,
    recommendation_body,
    reply_subject,
    stock_body,
)
from ..services.reviews import ReviewService
from .runner import JobClaim, PermanentJobError

logger = logging.getLogger("resilinet.workers.handlers")

MATCHING_KINDS = frozenset({ReportKind.NEED, ReportKind.SUPPLY})
NOTICE_KINDS = frozenset(
    {ReportKind.QUERY, ReportKind.STATUS_UPDATE, ReportKind.CANCEL, ReportKind.OTHER}
)


@dataclass(frozen=True, slots=True)
class _MatchingWork:
    """What the post-registration matching phase still has to do."""

    reply_message_id: UUID
    incident_id: UUID
    need_ids: tuple[UUID, ...]
    rematch_need_ids: tuple[UUID, ...] = ()


class MessageJobHandler:
    """Dispatch the two Phase 1 job types against the durable message store."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        inbox: InboxProvider,
        extraction: ExtractionService,
        notifications: NotificationService,
        intelligence: IntelligenceProvider,
        public_base_url: str = "http://localhost:8000",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._inbox = inbox
        self._extraction = extraction
        self._notifications = notifications
        self._clock = clock or utcnow
        self._allocations = AllocationService(clock=self._clock)
        self._critic = CriticService(intelligence)
        self._public_base_url = public_base_url.rstrip("/")

    async def __call__(self, claim: JobClaim) -> None:
        if claim.type is JobType.PROCESS_MESSAGE:
            await self._process(claim)
        elif claim.type is JobType.FETCH_CONTENT:
            await self._fetch(claim)
        else:  # pragma: no cover - the runner only registers these two
            raise PermanentJobError(f"unsupported job type {claim.type.value}")

    # -- process -----------------------------------------------------------

    async def _process(self, claim: JobClaim) -> None:
        message_id = self._message_id(claim)
        snapshot = await self._load_message(message_id)
        if snapshot is None:
            raise PermanentJobError("message no longer exists")
        message, sender = snapshot
        if message.status is MessageStatus.PROCESSED:
            # Registration committed but matching may not have: rebuild it.
            work = await self._recovery_work(message)
            if work is not None:
                await self._run_matching(work)
            return
        if message.status is not MessageStatus.QUEUED:
            # Already handled, throttled, deferred, or under review.
            return

        findings: list[InjectionFinding] = []
        try:
            parsed = await self._extraction.parse(
                _raw_message(message, sender), audit=findings.append
            )
        finally:
            try:
                await self._record_injection_findings(claim, findings)
            except Exception:
                logger.exception(
                    "injection_audit_write_failed", extra={"job_id": str(claim.id)}
                )

        now = self._clock()
        matching: _MatchingWork | None = None
        async with self._session_factory() as session, session.begin():
            current = await session.get(Message, message_id, with_for_update=True)
            if current is None:
                raise PermanentJobError("message no longer exists")
            if current.status is not MessageStatus.QUEUED:
                return
            clarification = ClarificationService(
                session, self._notifications, clock=lambda: now
            )
            original = (
                await session.get(
                    Message, current.reply_to_message_id, with_for_update=True
                )
                if current.reply_to_message_id is not None
                else None
            )
            if original is not None:
                await self._handle_correction(clarification, current, original, parsed)
                # Only a correction that actually merged in this transaction
                # may enter matching. Late, second, or wrong-sender replies
                # were routed to review by _handle_correction and must never
                # re-register reports or receive a recommendation. (Status
                # is re-read: _handle_correction mutated the row.)
                status_after = await session.scalar(
                    sa.select(Message.status).where(Message.id == current.id)
                )
                if status_after is MessageStatus.PROCESSED:
                    matching = await self._correction_work(session, original, current)
            else:
                matching = await self._handle_new(session, clarification, current, parsed)
        if matching is not None:
            await self._run_matching(matching)

    async def _handle_new(
        self,
        session: AsyncSession,
        clarification: ClarificationService,
        message: Message,
        parsed: ParsedEmail | ClarificationRequired,
    ) -> _MatchingWork | None:
        if isinstance(parsed, ClarificationRequired):
            if parsed.parsed is None:
                await clarification.escalate(
                    message,
                    None,
                    reason="extraction could not retain a partial report",
                    event_type="clarification.state_missing",
                    notice=self._notifications.human_review_text(
                        "the report could not be parsed"
                    ),
                )
                return None
            await clarification.ask(
                message, clarification.outcome_for_required(parsed), parsed.parsed
            )
            return None

        decision = clarification.evaluate(parsed, message)
        if isinstance(decision, ClarificationOutcome):
            await clarification.ask(message, decision, parsed)
            return None

        if parsed.context.kind in MATCHING_KINDS:
            persisted = await clarification.process_ready(message, parsed)
            sender = await session.get(Sender, message.sender_id)
            if sender is None:  # pragma: no cover - FK guarantees the row
                raise PermanentJobError("message sender disappeared")
            result = await self._allocations.register_reports(
                session,
                message=message,
                sender=sender,
                parsed=parsed,
                entity_id=persisted.entity_id,
                report_ids=list(persisted.report_ids),
                medical_flags=self._medical_flags(parsed),
            )
            await self._registration_notices(session, message, sender, result)
            return await self._work_from_registration(
                session, message, parsed, result
            )

        if parsed.context.kind not in NOTICE_KINDS:  # pragma: no cover - enum is closed
            raise PermanentJobError(f"unhandled report kind {parsed.context.kind.value}")
        message.status = MessageStatus.PROCESSED
        message.parsed_json = parsed.model_dump(mode="json")
        message.processed_at = self._clock()
        body = (
            self._notifications.query_text()
            if parsed.context.kind in (ReportKind.QUERY, ReportKind.OTHER)
            else self._notifications.action_required_text()
        )
        await self._enqueue_notice(
            session,
            message,
            kind=f"notice:{parsed.context.kind.value.lower()}",
            body=body,
        )
        await record_event(
            session,
            incident_id=message.incident_id,
            event_type="message.noted",
            aggregate_type="message",
            aggregate_id=message.id,
            payload={"message_id": str(message.id), "kind": parsed.context.kind.value},
        )
        return None

    async def _handle_correction(
        self,
        clarification: ClarificationService,
        correction: Message,
        original: Message,
        parsed: ParsedEmail | ClarificationRequired,
    ) -> None:
        if isinstance(parsed, ClarificationRequired):
            if parsed.parsed is None:
                await clarification.escalate(
                    original,
                    correction,
                    reason="correction could not be parsed",
                    event_type="clarification.escalated",
                    notice=self._notifications.human_review_text(),
                )
                return
            requested = set(original.clarification_fields)
            blocking = set(parsed.fields) & (requested | {SPLIT_FIELD})
            if blocking:
                await clarification.escalate(
                    original,
                    correction,
                    reason="correction is still ambiguous: "
                    + ", ".join(sorted(blocking)),
                    event_type="clarification.escalated",
                    notice=self._notifications.human_review_text(),
                )
                return
            correction_parsed = parsed.parsed
        else:
            correction_parsed = parsed
        await clarification.handle_correction(original, correction, correction_parsed)

    async def _correction_work(
        self,
        session: AsyncSession,
        original: Message,
        correction: Message,
    ) -> _MatchingWork | None:
        """Register a just-merged correction and queue its matching work."""
        if original.status is not MessageStatus.PROCESSED or original.parsed_json is None:
            return None
        merged = ParsedEmail.model_validate(original.parsed_json)
        if merged.context.kind not in MATCHING_KINDS:
            return None
        reports = list(
            await session.scalars(
                sa.select(Report)
                .where(Report.message_id == original.id)
                .order_by(Report.created_at)
            )
        )
        if not reports:
            return None
        sender = await session.get(Sender, original.sender_id)
        if sender is None:  # pragma: no cover - FK guarantees the row
            raise PermanentJobError("message sender disappeared")
        result = await self._allocations.register_reports(
            session,
            message=original,
            sender=sender,
            parsed=merged,
            entity_id=reports[0].entity_id,
            report_ids=[report.id for report in reports],
            medical_flags=self._medical_flags(merged),
        )
        await self._registration_notices(session, correction, sender, result)
        return await self._work_from_registration(session, correction, merged, result)

    # -- registration outcomes -------------------------------------------------

    async def _work_from_registration(
        self,
        session: AsyncSession,
        message: Message,
        parsed: ParsedEmail,
        result: RegistrationResult,
    ) -> _MatchingWork:
        need_ids = tuple(
            need.id for need in result.created_needs if not need.needs_human_review
        )
        rematch_ids: list[UUID] = []
        if parsed.context.kind is ReportKind.SUPPLY:
            now = self._clock()
            seen = set(need_ids)
            for snapshot in result.snapshots:
                ranked = await self._allocations.ranked_open_needs(
                    session, message.incident_id, snapshot.supply.resource, now
                )
                for need in ranked:
                    if need.id not in seen:
                        seen.add(need.id)
                        rematch_ids.append(need.id)
            if result.snapshots:
                entity = await session.get(
                    Entity, result.snapshots[0].supply.entity_id
                )
                lines = [
                    f"{_amount_label(snapshot.supply.on_hand)} "
                    f"{snapshot.supply.unit} of "
                    f"{snapshot.supply.resource.value.replace('_', ' ')}"
                    for snapshot in result.snapshots
                ]
                sender = await session.get(Sender, message.sender_id)
                if sender is not None and entity is not None:
                    await self._enqueue_guarded(
                        session,
                        message=message,
                        sender=sender,
                        kind="stock_update",
                        body=stock_body(entity.name, lines),
                    )
        return _MatchingWork(
            reply_message_id=message.id,
            incident_id=message.incident_id,
            need_ids=need_ids,
            rematch_need_ids=tuple(rematch_ids),
        )

    async def _registration_notices(
        self,
        session: AsyncSession,
        message: Message,
        sender: Sender,
        result: RegistrationResult,
    ) -> None:
        """Sender/operator outcomes that do not depend on matching."""
        reviews = ReviewService(session)
        for hit in result.duplicate_candidates:
            await reviews.escalate(
                incident_id=message.incident_id,
                kind=ReviewCaseKind.DUPLICATE,
                reason="nearby similar need may be a duplicate; never merged "
                "automatically",
                need_id=hit.need.id,
                payload={"existing_need_id": str(hit.existing_id)},
            )
        for need in result.created_needs:
            if not need.needs_human_review:
                continue
            await reviews.escalate(
                incident_id=message.incident_id,
                kind=ReviewCaseKind.MEDICAL,
                reason="medical escalation requires human review",
                need_id=need.id,
            )
            await self._enqueue_guarded(
                session,
                message=message,
                sender=sender,
                kind=f"medical_notice:{need.ref}",
                body=self._notifications.medical_review_text(),
            )
        medical_corroborated = {need.id for need in result.medical_corroborated}
        for need in result.corroborated:
            if need.id in medical_corroborated:
                # A medical corroboration still escalates: no stock promise
                # may be inferred from the existing need's match.
                await reviews.escalate(
                    incident_id=message.incident_id,
                    kind=ReviewCaseKind.MEDICAL,
                    reason="medical corroborating report requires human review",
                    need_id=need.id,
                )
                await self._enqueue_guarded(
                    session,
                    message=message,
                    sender=sender,
                    kind=f"medical_notice:{need.ref}",
                    body=self._notifications.medical_review_text(),
                )
                continue
            entity = await session.get(Entity, need.entity_id)
            await self._enqueue_guarded(
                session,
                message=message,
                sender=sender,
                kind=f"duplicate_notice:{need.ref}",
                body=self._notifications.duplicate_corroboration_text(
                    need_ref=need.ref,
                    entity_name=entity.name if entity is not None else "your shelter",
                    resource_label=need.resource.value.replace("_", " "),
                ),
            )
        if result.failures:
            await self._enqueue_guarded(
                session,
                message=message,
                sender=sender,
                kind="quantity_review",
                body=self._notifications.quantity_review_text(
                    resource_labels=[
                        failure.resource.value.replace("_", " ")
                        for failure in result.failures
                    ]
                ),
            )
            for failure in result.failures:
                await reviews.escalate(
                    incident_id=message.incident_id,
                    kind=ReviewCaseKind.QUANTITY,
                    reason=f"quantity could not be accepted: {failure.reason}",
                    report_id=failure.report_id,
                )
        for snapshot in result.snapshots:
            for allocation in snapshot.cancelled:
                affected = await session.get(Need, allocation.need_id)
                if affected is None:
                    continue
                source_message, source_sender = await self._need_contacts(
                    session, affected, fallback=(message, sender)
                )
                await self._enqueue_guarded(
                    session,
                    message=source_message,
                    sender=source_sender,
                    kind=f"allocation_cancelled:{allocation.ref}",
                    body=self._notifications.allocation_cancelled_text(
                        allocation_ref=allocation.ref, need_ref=affected.ref
                    ),
                )

    # -- matching phase -----------------------------------------------------------

    async def _recovery_work(self, message: Message) -> _MatchingWork | None:
        """Rebuild unfinished matching for an already-processed message."""
        async with self._session_factory() as session:
            completed = list(
                await session.scalars(
                    sa.select(EventLog)
                    .where(
                        EventLog.incident_id == message.incident_id,
                        EventLog.event_type == "matching.completed",
                    )
                    .order_by(EventLog.created_at.desc())
                    .limit(500)
                )
            )
            if any(
                event.payload.get("message_id") == str(message.id)
                for event in completed
            ):
                return None
            report_message_ids = [message.id]
            if message.reply_to_message_id is not None:
                report_message_ids.append(message.reply_to_message_id)
            needs = list(
                await session.scalars(
                    sa.select(Need)
                    .join(Report, Report.id == Need.source_report_id)
                    .where(
                        Report.message_id.in_(report_message_ids),
                        Need.status.in_((NeedStatus.OPEN, NeedStatus.PARTIAL)),
                    )
                )
            )
            reports = list(
                await session.scalars(
                    sa.select(Report).where(
                        Report.message_id.in_(report_message_ids)
                    )
                )
            )
            rematch_ids: list[UUID] = []
            now = self._clock()
            for resource in {report.resource for report in reports}:
                if any(report.kind is ReportKind.SUPPLY for report in reports):
                    ranked = await self._allocations.ranked_open_needs(
                        session, message.incident_id, resource, now
                    )
                    rematch_ids.extend(need.id for need in ranked)
            need_ids = tuple(
                need.id for need in needs if not need.needs_human_review
            )
            if not need_ids and not rematch_ids:
                return None
            return _MatchingWork(
                reply_message_id=message.id,
                incident_id=message.incident_id,
                need_ids=need_ids,
                rematch_need_ids=tuple(
                    need_id for need_id in rematch_ids if need_id not in need_ids
                ),
            )

    async def _run_matching(self, work: _MatchingWork) -> None:
        now = self._clock()
        ordered = list(work.need_ids) + [
            need_id
            for need_id in work.rematch_need_ids
            if need_id not in work.need_ids
        ]
        if not ordered:
            return

        # Drafts are built in a short read session; the bounded critic (LLM)
        # runs only after that session is closed, so no transaction or
        # connection is ever held across provider latency.
        plans: dict[UUID, ApprovedDrafts | NeedsReview | None] = {}
        prepared: dict[
            UUID, tuple[DraftValidationContext, CriticContext, list[AllocationDraft]]
        ] = {}
        async with self._session_factory() as session:
            for need_id in ordered:
                need = await session.get(Need, need_id)
                if need is None or need.needs_human_review:
                    continue
                if need.status is NeedStatus.CANCELLED:
                    continue
                drafts = await self._allocations.drafts_for_need(session, need_id, now)
                if not drafts:
                    plans[need_id] = None
                    continue
                validation, context = await self._critic.prepare(
                    session, self._allocations, need, drafts=drafts
                )
                prepared[need_id] = (validation, context, drafts)
        for need_id, (validation, context, drafts) in prepared.items():
            plans[need_id] = await self._critic.review(
                validation=validation, context=context, drafts=drafts
            )

        async with self._session_factory() as session, session.begin():
            reply_message = await session.get(Message, work.reply_message_id)
            if reply_message is None:  # pragma: no cover - FK guarantees the row
                raise PermanentJobError("reply target message disappeared")
            reply_sender = await session.get(Sender, reply_message.sender_id)
            if reply_sender is None:  # pragma: no cover - FK guarantees the row
                raise PermanentJobError("reply target sender disappeared")
            reviews = ReviewService(session)
            created_lines: list[RecommendationText] = []
            for need_id in ordered:
                if need_id not in plans:
                    continue
                need = await session.get(Need, need_id)
                if need is None or need.status is NeedStatus.CANCELLED:
                    continue
                plan = plans[need_id]
                source_message, source_sender = await self._need_contacts(
                    session, need, fallback=(reply_message, reply_sender)
                )
                if isinstance(plan, NeedsReview):
                    case = await reviews.escalate(
                        incident_id=need.incident_id,
                        kind=ReviewCaseKind.CRITIC,
                        reason=plan.reason,
                        need_id=need.id,
                        payload={
                            "violations": [v.code for v in plan.violations],
                            "llm_verdict": plan.llm_verdict,
                        },
                    )
                    await self._notify_operators(
                        session,
                        incident_id=need.incident_id,
                        case_id=case.id,
                        mailbox_id=reply_message.mailbox_id,
                    )
                    await self._enqueue_guarded(
                        session,
                        message=source_message,
                        sender=source_sender,
                        kind=f"review_notice:{need.ref}",
                        body=self._notifications.human_review_text(),
                    )
                    continue
                if isinstance(plan, ApprovedDrafts) and plan.drafts:
                    result = await self._allocations.commit(
                        session,
                        need_id=need.id,
                        drafts=plan.drafts,
                        critic_verdict=plan.verdict,
                        actor_id=source_sender.id,
                    )
                    for allocation in result.allocations:
                        await self._grant_or_review(
                            session,
                            reviews,
                            allocation,
                            need,
                            now,
                            mailbox_id=reply_message.mailbox_id,
                        )
                if need_id in work.need_ids:
                    created_lines.extend(
                        await self._recommendation_lines(session, need)
                    )
                elif plan is not None:
                    lines = await self._recommendation_lines(session, need)
                    await self._enqueue_guarded(
                        session,
                        message=source_message,
                        sender=source_sender,
                        kind=f"recommendation_update:{need.ref}",
                        body=recommendation_body(
                            lines, unverified=not source_sender.verified
                        ),
                    )
            if created_lines:
                await self._enqueue_guarded(
                    session,
                    message=reply_message,
                    sender=reply_sender,
                    kind="recommendation",
                    body=recommendation_body(
                        created_lines, unverified=not reply_sender.verified
                    ),
                )
            await record_event(
                session,
                incident_id=work.incident_id,
                event_type="matching.completed",
                aggregate_type="message",
                aggregate_id=reply_message.id,
                payload={
                    "message_id": str(work.reply_message_id),
                    "need_ids": [str(need_id) for need_id in ordered],
                },
            )

    async def _recommendation_lines(
        self, session: AsyncSession, need: Need
    ) -> list[RecommendationText]:
        """One truthful line per allocation (a split is never merged)."""
        entity = await session.get(Entity, need.entity_id)
        entity_name = entity.name if entity is not None else "your shelter"
        resource_label = need.resource.value.replace("_", " ")
        estimated = need.quantity_source is QuantitySource.ESTIMATED
        all_allocations = await self._allocations.allocations_for_need(session, need.id)
        active = [
            allocation
            for allocation in all_allocations
            if allocation.status in ACTIVE_ALLOCATION_STATUSES
        ]
        delivered = sum(
            (allocation.delivered_quantity for allocation in all_allocations),
            Decimal("0"),
        )
        if not active:
            return [
                RecommendationText(
                    need_ref=need.ref,
                    entity_name=entity_name,
                    resource_label=resource_label,
                    quantity_label=_quantity_label(
                        need.quantity - delivered, need.unit, estimated
                    ),
                    estimated=estimated,
                    headcount=need.headcount,
                )
            ]
        reserved_total = sum(
            (a.quantity - a.delivered_quantity for a in active), Decimal("0")
        )
        partial = delivered + reserved_total < need.quantity
        lines: list[RecommendationText] = []
        for index, allocation in enumerate(active):
            supply = await session.get(Supply, allocation.supply_id)
            supply_entity = (
                await session.get(Entity, supply.entity_id)
                if supply is not None
                else None
            )
            lines.append(
                RecommendationText(
                    need_ref=need.ref,
                    entity_name=entity_name,
                    resource_label=resource_label,
                    quantity_label=_quantity_label(
                        allocation.quantity - allocation.delivered_quantity,
                        need.unit,
                        estimated,
                    ),
                    allocation_ref=allocation.ref,
                    supply_name=(
                        supply_entity.name if supply_entity is not None else None
                    ),
                    distance_km=allocation.distance_km,
                    requested_label=(
                        _quantity_label(need.quantity, need.unit, estimated)
                        if partial and index == 0
                        else None
                    ),
                    estimated=estimated,
                    headcount=need.headcount,
                )
            )
        return lines

    async def _need_contacts(
        self,
        session: AsyncSession,
        need: Need,
        *,
        fallback: tuple[Message, Sender],
    ) -> tuple[Message, Sender]:
        """The original message/sender for a need, for threaded updates."""
        if need.source_report_id is not None:
            report = await session.get(Report, need.source_report_id)
            if report is not None:
                message = await session.get(Message, report.message_id)
                sender = await session.get(Sender, report.sender_id)
                if message is not None and sender is not None:
                    return message, sender
        return fallback

    async def _grant_or_review(
        self,
        session: AsyncSession,
        reviews: ReviewService,
        allocation: Allocation,
        need: Need,
        now: datetime,
        *,
        mailbox_id: str,
    ) -> None:
        """Hand a committed proposal to its depot coordinator, privately.

        The confirm grant is emailed only to the assigned verified depot
        coordinator; without one, a role-assignment review case is created
        instead of exposing the grant anywhere else.
        """
        supply = await session.get(Supply, allocation.supply_id)
        if supply is None:  # pragma: no cover - FK guarantees the row
            return
        coordinator = await session.scalar(
            sa.select(Sender)
            .join(ParticipantRole, ParticipantRole.sender_id == Sender.id)
            .where(
                ParticipantRole.incident_id == need.incident_id,
                ParticipantRole.role == ParticipantRoleKind.DEPOT_COORDINATOR,
                ParticipantRole.entity_id == supply.entity_id,
                Sender.verified.is_(True),
            )
            .order_by(Sender.address)
        )
        if coordinator is None:
            case = await reviews.escalate(
                incident_id=need.incident_id,
                kind=ReviewCaseKind.ROLE_ASSIGNMENT,
                reason="no verified depot coordinator is assigned to the "
                "supply depot",
                need_id=need.id,
                allocation_id=allocation.id,
            )
            await self._notify_operators(
                session,
                incident_id=need.incident_id,
                case_id=case.id,
                mailbox_id=mailbox_id,
            )
            return
        existing = await session.scalar(
            sa.select(ActionGrant).where(
                ActionGrant.allocation_id == allocation.id,
                ActionGrant.kind == PendingActionKind.CONFIRM_ALLOCATION,
                ActionGrant.consumed_at.is_(None),
                ActionGrant.expires_at > now,
            )
        )
        if existing is not None:
            return  # a live grant was already issued for this allocation
        # The 30-minute TTL starts when the grant is issued (after any LLM
        # critique), not when matching began; an expired grant is reissued.
        _grant, token = await create_action_grant(
            session,
            kind=PendingActionKind.CONFIRM_ALLOCATION,
            incident_id=need.incident_id,
            sender_id=coordinator.id,
            entity_id=supply.entity_id,
            need_id=need.id,
            allocation_id=allocation.id,
            now=self._clock(),
        )
        supply_entity = await session.get(Entity, supply.entity_id)
        need_entity = await session.get(Entity, need.entity_id)
        body = self._notifications.confirm_request_text(
            allocation_ref=allocation.ref,
            need_ref=need.ref,
            supply_entity=supply_entity.name if supply_entity is not None else "your depot",
            need_entity=need_entity.name if need_entity is not None else "a shelter",
            quantity_label=_quantity_label(
                allocation.quantity, allocation.unit, False
            ),
            resource_label=need.resource.value.replace("_", " "),
            action_url=f"{self._public_base_url}/actions/{token}",
        )
        await OutboxService.enqueue_direct(
            session,
            incident_id=need.incident_id,
            recipient=coordinator,
            mailbox_id=mailbox_id,
            subject=f"Confirm allocation {allocation.ref} for {need.ref}",
            body=body,
            kind=f"confirm_request:{allocation.ref}",
            available_at=now,
        )
        await record_event(
            session,
            incident_id=need.incident_id,
            event_type="allocation.confirm_requested",
            aggregate_type="allocation",
            aggregate_id=allocation.id,
            payload={
                "allocation_ref": allocation.ref,
                "need_ref": need.ref,
                "coordinator_sender_id": str(coordinator.id),
            },
        )

    async def _notify_operators(
        self,
        session: AsyncSession,
        *,
        incident_id: UUID,
        case_id: UUID,
        mailbox_id: str,
    ) -> None:
        """Email operators a non-token review-queue link (never an action link)."""
        operators = list(
            await session.scalars(
                sa.select(Sender)
                .join(ParticipantRole, ParticipantRole.sender_id == Sender.id)
                .where(
                    ParticipantRole.incident_id == incident_id,
                    ParticipantRole.role == ParticipantRoleKind.OPERATOR,
                )
                .order_by(Sender.address)
            )
        )
        kind = f"review_link:{case_id}"
        for operator in operators:
            queued = await session.scalar(
                sa.select(OutboundEmail.id).where(
                    OutboundEmail.kind == kind,
                    OutboundEmail.to_address == operator.address,
                )
            )
            if queued is not None:
                continue
            await OutboxService.enqueue_direct(
                session,
                incident_id=incident_id,
                recipient=operator,
                mailbox_id=mailbox_id,
                subject="Operator review needed",
                body=self._notifications.operator_review_text(
                    review_url=f"{self._public_base_url}/operator/reviews"
                ),
                kind=kind,
                available_at=self._clock(),
            )

    # -- outbox helpers -----------------------------------------------------------

    async def _enqueue_guarded(
        self,
        session: AsyncSession,
        *,
        message: Message,
        sender: Sender,
        kind: str,
        body: str,
    ) -> None:
        """Enqueue a threaded reply unless this logical notice already exists."""
        queued = await session.scalar(
            sa.select(OutboundEmail.id).where(
                OutboundEmail.message_id == message.id,
                OutboundEmail.kind == kind,
            )
        )
        if queued is not None:
            return
        await OutboxService.enqueue_reply(
            session,
            message=message,
            sender=sender,
            subject=reply_subject(message.subject),
            body=body,
            kind=kind,
            available_at=self._clock(),
        )

    async def _enqueue_notice(
        self,
        session: AsyncSession,
        message: Message,
        *,
        kind: str,
        body: str,
    ) -> None:
        sender = await session.get(Sender, message.sender_id)
        if sender is None:  # pragma: no cover - FK guarantees the row
            raise PermanentJobError("message sender disappeared")
        await OutboxService.enqueue_reply(
            session,
            message=message,
            sender=sender,
            subject=reply_subject(message.subject),
            body=body,
            kind=kind,
            available_at=self._clock(),
        )

    # -- fetch -------------------------------------------------------------

    async def _fetch(self, claim: JobClaim) -> None:
        message_id = self._message_id(claim)
        snapshot = await self._load_message(message_id)
        if snapshot is None:
            raise PermanentJobError("message no longer exists")
        message, _ = snapshot
        if message.status is not MessageStatus.DEFERRED:
            return
        try:
            fetched = await self._inbox.fetch_message(message.provider_message_id)
        except MessageNotFoundError as exc:
            raise PermanentJobError(f"provider no longer has the message: {exc}") from exc

        body = normalize_inbound_text(fetched.text)
        if fetched.withheld:
            # Partial content is not authoritative: keep the message deferred
            # and retry until the provider releases the body.
            raise ProviderUnavailableError(
                "provider still withholds the message content", retryable=True
            )

        now = self._clock()
        async with self._session_factory() as session, session.begin():
            current = await session.get(Message, message_id, with_for_update=True)
            if current is None:
                raise PermanentJobError("message no longer exists")
            if current.status is not MessageStatus.DEFERRED:
                return
            current.raw_text = fetched.text
            current.body_text = body
            current.withheld = fetched.withheld
            current.attachment_count = fetched.attachment_count
            current.status = MessageStatus.QUEUED
            session.add(
                Job(
                    incident_id=current.incident_id,
                    type=JobType.PROCESS_MESSAGE,
                    payload={"message_id": str(current.id)},
                    status=JobStatus.PENDING,
                    dedupe_key=f"process:{current.id}",
                    available_at=now,
                )
            )
            await record_event(
                session,
                incident_id=current.incident_id,
                event_type="message.content_fetched",
                aggregate_type="message",
                aggregate_id=current.id,
                payload={"message_id": str(current.id)},
            )

    # -- helpers -----------------------------------------------------------

    def _medical_flags(self, parsed: ParsedEmail) -> list[bool]:
        flags: list[bool] = []
        for report in parsed.reports:
            text = " ".join(
                part for part in (report.raw_text, report.qualifier) if part
            )
            flags.append(is_medical_report(report.resource, text or None))
        return flags

    async def _load_message(self, message_id: UUID) -> tuple[Message, Sender] | None:
        async with self._session_factory() as session:
            message = await session.get(Message, message_id)
            if message is None:
                return None
            sender = await session.get(Sender, message.sender_id)
            if sender is None:  # pragma: no cover - FK guarantees the row
                raise PermanentJobError("message sender disappeared")
            return message, sender

    async def _record_injection_findings(
        self, claim: JobClaim, findings: list[InjectionFinding]
    ) -> None:
        if not findings:
            return
        async with self._session_factory() as session, session.begin():
            for finding in findings:
                await record_event(
                    session,
                    incident_id=finding.incident_id,
                    event_type="security.extraction_injection",
                    trace_id=finding.trace_id,
                    payload={
                        "patterns": list(finding.patterns),
                        "message_ref": finding.message_ref,
                    },
                )
        logger.warning(
            "extraction_injection_detected",
            extra={
                "job_id": str(claim.id),
                "patterns": ",".join(
                    sorted({pattern for finding in findings for pattern in finding.patterns})
                ),
            },
        )

    @staticmethod
    def _message_id(claim: JobClaim) -> UUID:
        value = claim.payload.get("message_id")
        if not isinstance(value, str):
            raise PermanentJobError("job payload has no message_id")
        try:
            return UUID(value)
        except ValueError as exc:
            raise PermanentJobError("job payload has an invalid message_id") from exc


def _raw_message(message: Message, sender: Sender) -> RawMessage:
    return RawMessage(
        provider_message_id=message.provider_message_id,
        mailbox_id=message.mailbox_id,
        sender=sender.address,
        subject=message.subject,
        text=message.body_text,
        received_at=message.received_at,
        thread_id=message.thread_id,
        attachment_count=message.attachment_count,
        withheld=message.withheld,
    )


def _amount_label(amount: Decimal) -> str:
    return f"{amount.normalize():f}"


def _quantity_label(amount: Decimal, unit: str, estimated: bool) -> str:
    try:
        canonical = CanonicalUnit(unit)
    except ValueError:
        prefix = "~" if estimated else ""
        return f"{prefix}{amount.normalize():f} {unit}"
    return format_quantity(
        Quantity(
            amount=amount,
            unit=canonical,
            source=QuantitySource.ESTIMATED if estimated else QuantitySource.STATED,
        )
    )


__all__ = ["NOTICE_KINDS", "MessageJobHandler"]

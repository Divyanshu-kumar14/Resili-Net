"""Phase 0 synchronous pipeline.

This is the smallest composition that exercises the real ports, schemas, and
domain rules. Later phases replace the in-memory services with durable ones;
the interface stays ``process(raw: RawMessage) -> ProcessingResult``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from ..domain.quantities import Quantity, QuantityError, estimate_need, to_canonical
from ..domain.references import ReferenceSequence
from ..ports import (
    Geocoder,
    InboxProvider,
    IntelligenceProvider,
    OutboundDraft,
    RawMessage,
    SentReceipt,
)
from ..schemas import (
    ClarificationRequired,
    ParsedEmail,
    ParsedReport,
    ReportContext,
    ReportKind,
)
from .allocations import (
    AllocationRecord,
    AllocationService,
    AvailabilityError,
    EntityRecord,
    NeedRecord,
)
from .extraction import ExtractionService
from .ingestion import MemoryIngestionService
from .notifications import NotificationService, RecommendationLine, StockLine
from .public import map_snapshot


class UnsupportedReportKind(Exception):
    """The Phase 0 slice only handles NEED and SUPPLY reports."""

    def __init__(self, kind: ReportKind) -> None:
        super().__init__(f"unsupported report kind in the Phase 0 slice: {kind.value}")
        self.kind = kind


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    kind: ReportKind = ReportKind.OTHER
    duplicate: bool = False
    need_ref: str | None = None
    allocation_ref: str | None = None
    reply: OutboundDraft | None = None
    sent: SentReceipt | None = None
    clarification_question: str | None = None
    public_projection: Mapping[str, Any] = field(default_factory=dict)


class Pipeline:
    def __init__(
        self,
        *,
        incident_id: UUID,
        inbox: InboxProvider,
        intelligence: IntelligenceProvider,
        geocoder: Geocoder,
        clock: Callable[[], datetime] | None = None,
        references: ReferenceSequence | None = None,
    ) -> None:
        self.incident_id = incident_id
        self._inbox = inbox
        self._clock = clock or (lambda: datetime.now(UTC))
        # Phase 0 slice only: deleted with the synchronous pipeline in Phase 2.
        self.ingestion = MemoryIngestionService(incident_id)
        self.extraction = ExtractionService(incident_id, intelligence, geocoder)
        self.allocations = AllocationService(clock=self._clock, references=references)
        self.notifications = NotificationService()
        self._results: dict[str, ProcessingResult] = {}
        self._pending: dict[str, tuple[OutboundDraft, str]] = {}

    async def process(self, raw: RawMessage) -> ProcessingResult:
        cached = self._results.get(raw.provider_message_id)
        if cached is not None:
            pending = self._pending.get(raw.provider_message_id)
            if pending is not None:
                draft, purpose = pending
                sent = await self._send(raw, draft, purpose=purpose)
                cached = replace(cached, sent=sent)
                self._results[raw.provider_message_id] = cached
                del self._pending[raw.provider_message_id]
            return cached

        accepted = await self.ingestion.accept(raw)
        if accepted.duplicate:
            return ProcessingResult(duplicate=True, public_projection=self.projection())

        parsed = await self.extraction.parse(raw)
        if isinstance(parsed, ClarificationRequired):
            return await self._clarify(
                raw, parsed.fields, parsed.kind, accepted.message_id
            )

        if parsed.context.kind is ReportKind.NEED:
            return await self._process_need(raw, parsed, accepted.message_id)
        if parsed.context.kind is ReportKind.SUPPLY:
            return await self._process_supply(raw, parsed, accepted.message_id)
        raise UnsupportedReportKind(parsed.context.kind)

    # -- report handlers --------------------------------------------------

    async def _process_supply(
        self, raw: RawMessage, parsed: ParsedEmail, message_id: UUID
    ) -> ProcessingResult:
        quantities: list[tuple[ParsedReport, Quantity]] = []
        for report in parsed.reports:
            quantity = self._canonical_or_none(report)
            if quantity is None:
                return await self._clarify(
                    raw, ["quantity"], ReportKind.SUPPLY, message_id
                )
            quantities.append((report, quantity))

        entity = self._record_entity(parsed.context)

        # Validate the whole compound message before changing any stock.
        try:
            for report, quantity in quantities:
                self.allocations.check_snapshot(
                    entity_id=entity.id,
                    resource=report.resource,
                    amount=quantity.amount,
                )
        except AvailabilityError:
            reply = self.notifications.compose_snapshot_rejected(raw, entity.name)
            result = ProcessingResult(kind=ReportKind.SUPPLY, reply=reply)
            return await self._commit_and_send(
                raw, message_id, result, reply, "snapshot-rejected"
            )

        stock_lines: list[StockLine] = []
        for report, quantity in quantities:
            self.allocations.upsert_supply(
                entity_id=entity.id,
                resource=report.resource,
                quantity=quantity,
            )
            stock_lines.append(StockLine(resource=report.resource, quantity=quantity))

        reply = self.notifications.compose_stock_update(raw, entity.name, stock_lines)
        result = ProcessingResult(kind=ReportKind.SUPPLY, reply=reply)
        return await self._commit_and_send(raw, message_id, result, reply, "reply")

    async def _process_need(
        self, raw: RawMessage, parsed: ParsedEmail, message_id: UUID
    ) -> ProcessingResult:
        quantities: list[tuple[ParsedReport, Quantity]] = []
        for report in parsed.reports:
            quantity = self._need_quantity(report, parsed.context.headcount)
            if quantity is None:
                fields = ["quantity"]
                if parsed.context.headcount is None:
                    fields.append("headcount")
                return await self._clarify(raw, fields, ReportKind.NEED, message_id)
            quantities.append((report, quantity))

        entity = self._record_entity(parsed.context)
        lines: list[RecommendationLine] = []
        primary: tuple[NeedRecord, tuple[AllocationRecord, ...]] | None = None
        for report, quantity in quantities:
            need = self.allocations.create_need(
                entity_id=entity.id,
                resource=report.resource,
                quantity=quantity,
                headcount=parsed.context.headcount,
                urgency=parsed.context.urgency,
                verified=False,
                needs_human_review=False,
            )
            allocations = self.allocations.propose(need.id)
            need = self.allocations.need(need.id)
            if primary is None:
                primary = (need, allocations)
            lines.append(self._recommendation_line(entity, need, allocations))

        if primary is None:
            raise UnsupportedReportKind(parsed.context.kind)
        reply = self.notifications.compose_recommendation(
            raw, lines, unverified=not primary[0].verified
        )
        need0, allocations0 = primary
        result = ProcessingResult(
            kind=ReportKind.NEED,
            need_ref=need0.ref,
            allocation_ref=allocations0[0].ref if allocations0 else None,
            reply=reply,
        )
        return await self._commit_and_send(raw, message_id, result, reply, "reply")

    # -- helpers ----------------------------------------------------------

    def _record_entity(self, context: ReportContext) -> EntityRecord:
        name = (context.entity_name or "").strip()
        if not name:
            raise UnsupportedReportKind(context.kind)
        return self.allocations.upsert_entity(
            name=name,
            entity_type=context.entity_type,
            lat=context.lat,
            lon=context.lon,
            geo_confidence=context.geo_confidence,
            location_text=context.location_text,
        )

    @staticmethod
    def _canonical_or_none(report: ParsedReport) -> Quantity | None:
        try:
            return to_canonical(report.resource, report.quantity, report.unit)
        except QuantityError:
            return None

    @staticmethod
    def _need_quantity(report: ParsedReport, headcount: int | None) -> Quantity | None:
        # A stated zero is missing information, not a request for nothing: use
        # the headcount estimate when available, otherwise ask one question.
        if report.quantity is not None and report.quantity > 0:
            try:
                return to_canonical(report.resource, report.quantity, report.unit)
            except QuantityError:
                return None
        return estimate_need(report.resource, headcount)

    def _recommendation_line(
        self,
        entity: EntityRecord,
        need: NeedRecord,
        allocations: tuple[AllocationRecord, ...],
    ) -> RecommendationLine:
        allocation_ref: str | None = None
        supply_name: str | None = None
        distance_km = None
        if allocations:
            top = allocations[0]
            supply_entity = self.allocations.entity(
                self.allocations.supply(top.supply_id).entity_id
            )
            allocation_ref = top.ref
            supply_name = supply_entity.name
            distance_km = top.distance_km
            quantity = Quantity(
                amount=top.quantity,
                unit=need.unit,
                source=need.quantity_source,
            )
            requested = Quantity(
                amount=need.quantity,
                unit=need.unit,
                source=need.quantity_source,
            )
        else:
            quantity = Quantity(
                amount=need.quantity,
                unit=need.unit,
                source=need.quantity_source,
            )
            requested = None
        return RecommendationLine(
            need_ref=need.ref,
            entity_name=entity.name,
            resource=need.resource,
            quantity=quantity,
            headcount=need.headcount,
            allocation_ref=allocation_ref,
            supply_name=supply_name,
            distance_km=distance_km,
            requested_quantity=requested,
        )

    async def _clarify(
        self,
        raw: RawMessage,
        fields: list[str],
        kind: ReportKind,
        message_id: UUID,
    ) -> ProcessingResult:
        reply = self.notifications.compose_clarification(raw, fields)
        result = ProcessingResult(
            kind=kind,
            reply=reply,
            clarification_question=reply.text,
        )
        return await self._commit_and_send(raw, message_id, result, reply, "reply")

    async def _commit_and_send(
        self,
        raw: RawMessage,
        message_id: UUID,
        result: ProcessingResult,
        draft: OutboundDraft,
        purpose: str,
    ) -> ProcessingResult:
        """Commit the domain result, then send exactly one logical notification.

        The result and pending draft are cached before the provider call. A
        retry of the same provider message re-sends the cached draft with the
        same idempotency key instead of re-running extraction or reservations.
        """
        key = raw.provider_message_id
        committed = replace(
            result,
            reply=draft,
            public_projection=self.projection(),
        )
        self._results[key] = committed
        self._pending[key] = (draft, purpose)
        self.ingestion.complete(message_id)

        sent = await self._send(raw, draft, purpose=purpose)
        completed = replace(committed, sent=sent)
        self._results[key] = completed
        del self._pending[key]
        return completed

    async def _send(
        self, raw: RawMessage, draft: OutboundDraft, *, purpose: str = "reply"
    ) -> SentReceipt:
        return await self._inbox.reply(
            raw.provider_message_id,
            draft,
            self._outbox_key(raw.provider_message_id, purpose),
        )

    def _outbox_key(self, provider_message_id: str, purpose: str) -> str:
        """Stable across retries; unique per logical notification."""
        digest = uuid5(
            NAMESPACE_URL,
            f"resilinet:{self.incident_id}:{provider_message_id}:{purpose}",
        )
        return f"outbox:{digest}"

    def projection(self) -> dict[str, Any]:
        supplies = self.allocations.all_supplies()
        available = {
            supply.id: self.allocations.available(supply.id) for supply in supplies
        }
        return map_snapshot(
            incident_id=self.incident_id,
            entities=self.allocations.all_entities(),
            needs=self.allocations.all_needs(),
            supplies=supplies,
            allocations=self.allocations.all_allocations(),
            available_by_supply=available,
            generated_at=self._clock(),
        )


__all__ = ["Pipeline", "ProcessingResult", "UnsupportedReportKind"]

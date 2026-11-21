"""Compound-report handling and one bounded clarification round.

Rules enforced here:

* several resources for one entity, one place, and one kind (NEED or SUPPLY)
  are valid in one message;
* a mixed entity/place/kind message asks the sender to split it, and nothing is
  attributed by guesswork;
* a missing resource, location, or non-estimable quantity produces exactly one
  question listing every missing field;
* a missing quantity with a known headcount is estimable and needs no question;
* a correction is merged only for the missing fields, only from the original
  sender, and only once—a second ambiguity becomes a human review case.

All writes happen inside the caller's transaction. Email delivery is queued as
an outbox row, never sent here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import utcnow
from ..models import Message, MessageStatus, ReviewCase, ReviewCaseKind, Sender
from ..schemas import ClarificationRequired, GeoConfidence, ParsedEmail, ReportKind
from .events import record_event
from .extraction import (
    ITEMS_CONFIDENCE_MIN,
    LOCATION_CONFIDENCE_MIN,
    MATCHED_KINDS,
    MAX_PLAUSIBLE_QUANTITY,
    PersistedReports,
    ReportPersistence,
    persisted_reports_payload,
)
from .notifications import NotificationService, OutboxService, reply_subject

RESOLVABLE_FIELDS = frozenset({"entity_name", "entity_type", "location", "quantity", "headcount"})
SPLIT_FIELD = "split"

#: Model-produced field names mapped onto the fields a correction can merge.
_FIELD_ALIASES: dict[str, str] = {
    "item": "quantity",
    "items": "quantity",
    "amount": "quantity",
    "quantity_missing": "quantity",
    "unit": "quantity",
    "place": "location",
    "location_missing": "location",
    "name": "entity_name",
    "entity": "entity_name",
    "people": "headcount",
    "persons": "headcount",
}


class MergeError(ValueError):
    """A correction cannot be merged without guessing."""


class ClarificationStateError(RuntimeError):
    """A message is not in a state that may accept a clarification outcome."""


def normalize_clarification_fields(fields: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Map model field names onto resolvable fields and drop empties."""
    normalized = []
    for field in fields:
        clean = field.strip().lower()
        if not clean:
            continue
        normalized.append(_FIELD_ALIASES.get(clean, clean))
    return tuple(dict.fromkeys(normalized))


@dataclass(frozen=True, slots=True)
class ReadyReports:
    parsed: ParsedEmail


@dataclass(frozen=True, slots=True)
class ClarificationOutcome:
    fields: tuple[str, ...]
    question: str
    kind: ReportKind
    reason: str | None = None
    review_required: bool = False


class ClarificationService:
    def __init__(
        self,
        session: AsyncSession,
        notifications: NotificationService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._notifications = notifications
        self._clock = clock or utcnow
        self._persistence = ReportPersistence(session)

    # -- decisions ---------------------------------------------------------

    def outcome_for_required(self, required: ClarificationRequired) -> ClarificationOutcome:
        fields = normalize_clarification_fields(required.fields)
        split = SPLIT_FIELD in fields
        question = (
            self._notifications.split_text()
            if split
            else self._notifications.clarification_text(list(fields))
        )
        return ClarificationOutcome(
            fields=fields,
            question=question,
            kind=required.kind,
            reason=required.reason,
            review_required=False,
        )

    def evaluate(
        self, parsed: ParsedEmail, message: Message | None = None
    ) -> ReadyReports | ClarificationOutcome:
        context = parsed.context
        fields: list[str] = list(parsed.needs_clarification)
        fields.extend(context.clarification_fields)

        if parsed.split_required:
            fields.append(SPLIT_FIELD)
        if context.kind in MATCHED_KINDS:
            if not (context.entity_name or "").strip():
                fields.append("entity_name")
            if context.geo_confidence is GeoConfidence.NONE:
                fields.append("location")
            if context.kind is ReportKind.NEED:
                missing_quantity = any(
                    report.quantity is None or report.quantity == 0
                    for report in parsed.reports
                )
                if missing_quantity and context.headcount is None:
                    fields.extend(["quantity", "headcount"])
            elif any(report.quantity is None for report in parsed.reports):
                fields.append("quantity")

        unique = normalize_clarification_fields(fields)
        if not unique:
            return ReadyReports(parsed=parsed)
        if SPLIT_FIELD in unique:
            question = self._notifications.split_text()
        else:
            question = self._notifications.clarification_text(list(unique))
        return ClarificationOutcome(
            fields=unique,
            question=question,
            kind=context.kind,
            reason=(
                "mixed entities, locations, or report kinds"
                if SPLIT_FIELD in unique
                else None
            ),
        )

    def merge(
        self,
        original: ParsedEmail,
        correction: ParsedEmail,
        missing: list[str],
    ) -> ParsedEmail:
        """Merge only the fields that were asked for; everything else is kept."""
        fields = set(missing)
        unsupported = fields - RESOLVABLE_FIELDS
        if unsupported:
            raise MergeError(
                "cannot merge unresolved fields: " + ", ".join(sorted(unsupported))
            )

        original_context = original.context
        corrected = correction.context
        updates: dict[str, object] = {"clarification_fields": []}
        confidence = original_context.confidence

        if "entity_name" in fields:
            name = (corrected.entity_name or "").strip()
            if not name:
                raise MergeError("entity name is still missing")
            updates["entity_name"] = name
            confidence = confidence.model_copy(
                update={"items": corrected.confidence.items}
            )
        if "entity_type" in fields:
            updates["entity_type"] = corrected.entity_type
        if "headcount" in fields:
            if corrected.headcount is None:
                raise MergeError("headcount is still missing")
            if corrected.confidence.headcount < ITEMS_CONFIDENCE_MIN:
                raise MergeError("headcount confidence is still too low")
            updates["headcount"] = corrected.headcount
        if "location" in fields:
            if corrected.lat is None or corrected.lon is None:
                raise MergeError("location is still unresolved")
            if corrected.confidence.location < LOCATION_CONFIDENCE_MIN:
                raise MergeError("location confidence is still too low")
            updates.update(
                lat=corrected.lat,
                lon=corrected.lon,
                geo_confidence=corrected.geo_confidence,
                location_source=corrected.location_source,
                location_text=corrected.location_text or original_context.location_text,
            )
            confidence = confidence.model_copy(
                update={"location": corrected.confidence.location}
            )

        reports = list(original.reports)
        if "quantity" in fields:
            if corrected.confidence.items < ITEMS_CONFIDENCE_MIN:
                raise MergeError("quantity confidence is still too low")
            merged_reports = []
            for report in reports:
                if report.quantity is not None and report.quantity > 0:
                    merged_reports.append(report)
                    continue
                candidates = [
                    candidate
                    for candidate in correction.reports
                    if candidate.resource is report.resource
                ]
                if len(candidates) != 1:
                    raise MergeError(
                        f"quantity for {report.resource.value} is still ambiguous"
                    )
                candidate = candidates[0]
                if candidate.quantity is None or candidate.quantity <= 0:
                    raise MergeError(
                        f"quantity for {report.resource.value} is still missing"
                    )
                if candidate.quantity > MAX_PLAUSIBLE_QUANTITY:
                    raise MergeError(
                        f"quantity for {report.resource.value} is implausibly large"
                    )
                merged_reports.append(
                    report.model_copy(
                        update={
                            "quantity": candidate.quantity,
                            "unit": candidate.unit,
                            "qualifier": candidate.qualifier,
                        }
                    )
                )
            reports = merged_reports
            confidence = confidence.model_copy(
                update={"items": corrected.confidence.items}
            )

        context = original_context.model_copy(
            update={**updates, "confidence": confidence}
        )
        return original.model_copy(
            update={
                "context": context,
                "reports": reports,
                "needs_clarification": [],
                "split_required": False,
            }
        )

    # -- writes ------------------------------------------------------------

    async def ask(
        self,
        message: Message,
        outcome: ClarificationOutcome,
        parsed: ParsedEmail | None = None,
    ) -> None:
        message.status = MessageStatus.CLARIFICATION_PENDING
        message.clarification_fields = list(outcome.fields)
        message.clarification_attempts += 1
        if parsed is not None:
            message.parsed_json = parsed.model_dump(mode="json")
        sender = await self._sender(message.sender_id)
        await OutboxService.enqueue_reply(
            self._session,
            message=message,
            sender=sender,
            subject=reply_subject(message.subject),
            body=outcome.question,
            kind="clarification",
            available_at=self._now(),
        )
        await record_event(
            self._session,
            incident_id=message.incident_id,
            event_type="clarification.requested",
            aggregate_type="message",
            aggregate_id=message.id,
            payload={"message_id": str(message.id), "fields": list(outcome.fields)},
        )

    async def process_ready(self, message: Message, parsed: ParsedEmail) -> PersistedReports:
        """Persist a complete report and mark its message processed."""
        if message.status not in (
            MessageStatus.QUEUED,
            MessageStatus.CLARIFICATION_PENDING,
        ):
            raise ClarificationStateError(
                f"cannot complete a message in state {message.status.value}"
            )
        result = await self._persistence.persist(
            incident_id=message.incident_id,
            message_id=message.id,
            sender_id=message.sender_id,
            parsed=parsed,
        )
        now = self._now()
        message.status = MessageStatus.PROCESSED
        message.parsed_json = parsed.model_dump(mode="json")
        message.clarification_fields = []
        message.processed_at = now
        await record_event(
            self._session,
            incident_id=message.incident_id,
            event_type="report.persisted",
            aggregate_type="message",
            aggregate_id=message.id,
            payload={
                "message_id": str(message.id),
                "kind": parsed.context.kind.value,
                **persisted_reports_payload(result, parsed),
            },
        )
        return result

    async def handle_correction(
        self,
        original: Message,
        correction: Message,
        correction_parsed: ParsedEmail,
    ) -> None:
        if original.status is not MessageStatus.CLARIFICATION_PENDING:
            # The report already moved on; never regress or complete it again.
            await self._late_correction(original, correction)
            return
        if correction.sender_id != original.sender_id:
            # The original report stays pending: the real sender can still
            # answer. Only the unsolicited reply becomes a review case.
            correction.status = MessageStatus.NEEDS_REVIEW
            review = ReviewCase(
                id=uuid4(),
                incident_id=original.incident_id,
                kind=ReviewCaseKind.CLARIFICATION,
                source_message_id=correction.id,
                reason="correction arrived from a different sender",
                payload={
                    "message_id": str(original.id),
                    "correction_message_id": str(correction.id),
                },
            )
            self._session.add(review)
            await self._session.flush()
            sender = await self._sender(correction.sender_id)
            await OutboxService.enqueue_reply(
                self._session,
                message=correction,
                sender=sender,
                subject=reply_subject(correction.subject),
                body=self._notifications.wrong_sender_text(),
                kind="clarification_review",
                available_at=self._now(),
            )
            await record_event(
                self._session,
                incident_id=original.incident_id,
                event_type="clarification.wrong_sender",
                aggregate_type="message",
                aggregate_id=correction.id,
                payload={
                    "message_id": str(original.id),
                    "correction_message_id": str(correction.id),
                    "review_case_id": str(review.id),
                },
            )
            return

        if original.parsed_json is None:
            await self.escalate(
                original,
                correction,
                reason="original clarification state is missing",
                event_type="clarification.state_missing",
                notice=self._notifications.human_review_text("missing report state"),
            )
            return

        prior_corrections = await self._session.scalar(
            sa.select(sa.func.count(Message.id)).where(
                Message.reply_to_message_id == original.id,
                Message.id != correction.id,
            )
        )
        if prior_corrections:
            await self.escalate(
                original,
                correction,
                reason="a second correction arrived for one clarification",
                event_type="clarification.second_correction",
                notice=self._notifications.human_review_text(),
            )
            return

        original_parsed = ParsedEmail.model_validate(original.parsed_json)
        missing = list(original.clarification_fields)
        try:
            merged = self.merge(original_parsed, correction_parsed, missing)
        except MergeError as exc:
            await self.escalate(
                original,
                correction,
                reason=f"second ambiguity: {exc}",
                event_type="clarification.escalated",
                notice=self._notifications.human_review_text(),
            )
            return

        outcome = self.evaluate(merged)
        if isinstance(outcome, ClarificationOutcome):
            await self.escalate(
                original,
                correction,
                reason="second ambiguity: " + ", ".join(outcome.fields),
                event_type="clarification.escalated",
                notice=self._notifications.human_review_text(),
            )
            return

        await self.process_ready(original, merged)
        correction.status = MessageStatus.PROCESSED
        correction.parsed_json = correction_parsed.model_dump(mode="json")
        correction.processed_at = self._now()
        sender = await self._sender(original.sender_id)
        await OutboxService.enqueue_reply(
            self._session,
            message=correction,
            sender=sender,
            subject=reply_subject(correction.subject),
            body=self._notifications.clarification_merged_text(),
            kind="clarification_merged",
            available_at=self._now(),
        )
        await record_event(
            self._session,
            incident_id=original.incident_id,
            event_type="clarification.merged",
            aggregate_type="message",
            aggregate_id=original.id,
            payload={
                "message_id": str(original.id),
                "correction_message_id": str(correction.id),
                "fields": missing,
            },
        )

    # -- helpers -----------------------------------------------------------

    async def _late_correction(self, original: Message, correction: Message) -> None:
        """Review a reply that arrived after the report left clarification.

        The original report is never modified: its reports are already
        persisted (or already escalated). Only the late reply becomes a review
        case, so the sender still gets a truthful, auditable outcome.
        """
        correction.status = MessageStatus.NEEDS_REVIEW
        review = ReviewCase(
            id=uuid4(),
            incident_id=original.incident_id,
            kind=ReviewCaseKind.CLARIFICATION,
            source_message_id=correction.id,
            reason=(
                "correction arrived while the report was "
                f"{original.status.value}"
            )[:500],
            payload={
                "message_id": str(original.id),
                "correction_message_id": str(correction.id),
            },
        )
        self._session.add(review)
        await self._session.flush()
        sender = await self._sender(correction.sender_id)
        await OutboxService.enqueue_reply(
            self._session,
            message=correction,
            sender=sender,
            subject=reply_subject(correction.subject),
            body=self._notifications.human_review_text(
                "the original report was already completed"
            ),
            kind="clarification_review",
            available_at=self._now(),
        )
        await record_event(
            self._session,
            incident_id=original.incident_id,
            event_type="clarification.late_correction",
            aggregate_type="message",
            aggregate_id=correction.id,
            payload={
                "message_id": str(original.id),
                "correction_message_id": str(correction.id),
                "original_status": original.status.value,
                "review_case_id": str(review.id),
            },
        )

    async def escalate(
        self,
        original: Message,
        correction: Message | None,
        *,
        reason: str,
        event_type: str,
        notice: str,
    ) -> None:
        original.status = (
            original.status
            if original.status is MessageStatus.PROCESSED
            else MessageStatus.NEEDS_REVIEW
        )
        if correction is not None and correction.status is not MessageStatus.PROCESSED:
            correction.status = MessageStatus.NEEDS_REVIEW
        review = ReviewCase(
            id=uuid4(),
            incident_id=original.incident_id,
            kind=ReviewCaseKind.CLARIFICATION,
            source_message_id=original.id,
            reason=reason[:500],
            payload={"message_id": str(original.id)},
        )
        self._session.add(review)
        await self._session.flush()
        sender = await self._sender(original.sender_id)
        await OutboxService.enqueue_reply(
            self._session,
            message=correction or original,
            sender=sender,
            subject=reply_subject((correction or original).subject),
            body=notice,
            kind="clarification_review",
            available_at=self._now(),
        )
        await record_event(
            self._session,
            incident_id=original.incident_id,
            event_type=event_type,
            aggregate_type="message",
            aggregate_id=original.id,
            payload={
                "message_id": str(original.id),
                "correction_message_id": (
                    str(correction.id) if correction is not None else None
                ),
                "review_case_id": str(review.id),
            },
        )

    async def _sender(self, sender_id: UUID) -> Sender:
        sender = await self._session.get(Sender, sender_id)
        if sender is None:  # pragma: no cover - FK guarantees the row
            raise LookupError("sender disappeared")
        return sender

    def _now(self) -> datetime:
        return self._clock()


__all__ = [
    "RESOLVABLE_FIELDS",
    "SPLIT_FIELD",
    "ClarificationOutcome",
    "ClarificationService",
    "MergeError",
    "ReadyReports",
]

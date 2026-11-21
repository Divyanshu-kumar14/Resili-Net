"""Extraction orchestration: validate, geocode, and persist validated reports.

The model output is already schema-validated by the adapter. This module adds
the deterministic gates that stop low-confidence, implausible, or unlocatable
data from reaching matching. It also records prompt-injection attempts as audit
findings; detection never changes the parsed result, it only makes the attempt
visible.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.quantities import QuantitySource
from ..models import Entity, Report
from ..observability import current_trace_id, new_trace_id
from ..ports import Geocoder, IntelligenceProvider, RawMessage
from ..schemas import (
    ClarificationRequired,
    EntityType,
    GeoConfidence,
    ParsedEmail,
    ReportContext,
    ReportKind,
)

LOCATION_CONFIDENCE_MIN = 0.60
ITEMS_CONFIDENCE_MIN = 0.75
MATCHED_KINDS = frozenset({ReportKind.NEED, ReportKind.SUPPLY})

#: Above this, a stated amount is implausible for a single relief report and
#: must be clarified rather than silently accepted. Water has a stricter
#: conversion-time cap in ``domain.quantities``.
MAX_PLAUSIBLE_QUANTITY = Decimal("1000000")


@dataclass(frozen=True, slots=True)
class InjectionFinding:
    """One sanitized prompt-injection audit record.

    It carries the matched pattern names, never the matched text, so the audit
    trail cannot be used to echo an attacker's instructions back into logs.
    """

    incident_id: UUID
    trace_id: str
    message_ref: str
    patterns: tuple[str, ...]


AuditSink = Callable[[InjectionFinding], None]

_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(
            r"\b(ignore|disregard|forget|override)\b.{0,40}"
            r"\b(previous|prior|above|system|developer)\b.{0,40}"
            r"\b(instruction|instructions|prompt|prompts|rule|rules)\b"
        ),
    ),
    (
        "bulk_release",
        re.compile(
            r"\b(send|release|dispatch|allocate|give)\b.{0,40}"
            r"\b(all|every|entire)\b.{0,40}"
            r"\b(water|food|stock|supplies|supply|inventory|blankets|medicine)\b"
        ),
    ),
    (
        "schema_override",
        re.compile(
            r"\b(change|modify|override|replace|bypass)\b.{0,40}"
            r"\b(schema|validation|rules?|output format|restrictions?)\b"
        ),
    ),
    (
        "tool_invocation",
        re.compile(
            r"\b(call|invoke|run|execute|use)\b.{0,40}"
            r"\b(tools?|functions?|shell|python|code execution|api)\b"
        ),
    ),
    (
        "role_override",
        re.compile(
            r"\b(you are now|act as|system prompt|developer message)\b|"
            r"<\|[^>]{1,40}\|>"
        ),
    ),
)


def detect_injection(text: str) -> tuple[str, ...]:
    """Return the names of injection patterns found in untrusted text."""
    haystack = " ".join(
        unicodedata.normalize("NFKC", text).casefold().split()
    )
    return tuple(name for name, pattern in _INJECTION_PATTERNS if pattern.search(haystack))


def normalize_entity_name(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


class ExtractionService:
    def __init__(
        self,
        incident_id: UUID,
        intelligence: IntelligenceProvider,
        geocoder: Geocoder,
        *,
        audit: AuditSink | None = None,
    ) -> None:
        self._incident_id = incident_id
        self._intelligence = intelligence
        self._geocoder = geocoder
        self._audit = audit

    async def parse(
        self,
        raw: RawMessage,
        *,
        audit: AuditSink | None = None,
    ) -> ParsedEmail | ClarificationRequired:
        sink = audit if audit is not None else self._audit
        patterns = detect_injection(f"{raw.subject}\n{raw.text}")
        if patterns and sink is not None:
            sink(
                InjectionFinding(
                    incident_id=self._incident_id,
                    trace_id=current_trace_id() or new_trace_id(),
                    message_ref=raw.provider_message_id,
                    patterns=patterns,
                )
            )

        parsed = await self._intelligence.extract(raw)
        context = parsed.context
        fields: list[str] = list(parsed.needs_clarification)
        fields.extend(context.clarification_fields)
        explicit_fields = set(fields)

        if parsed.split_required:
            fields.append("split")
        if context.kind in MATCHED_KINDS:
            if not (context.entity_name or "").strip():
                fields.append("entity_name")
            if context.confidence.items < ITEMS_CONFIDENCE_MIN:
                fields.append("items")
        if context.confidence.location < LOCATION_CONFIDENCE_MIN:
            fields.append("location")
        if any(
            report.quantity is not None
            and report.quantity > MAX_PLAUSIBLE_QUANTITY
            for report in parsed.reports
        ):
            fields.append("quantity")

        has_location = bool((context.location_text or "").strip())
        has_coordinates = context.lat is not None and context.lon is not None
        if not parsed.split_required and (has_location or has_coordinates):
            context = await self._resolve_location(context)
            if (
                context.kind in MATCHED_KINDS
                and context.geo_confidence is GeoConfidence.NONE
            ):
                fields.append("location")
            elif (
                context.geo_confidence is GeoConfidence.EXACT
                and "location" not in explicit_fields
            ):
                # A curated gazetteer match or explicit coordinates are
                # authoritative; do not ask the sender to repeat a location the
                # system already knows. This also geocodes location-only
                # corrections whose kind is not a matched report.
                fields = [field for field in fields if field != "location"]

        unique_fields = list(dict.fromkeys(fields))
        if unique_fields:
            return ClarificationRequired(
                kind=context.kind,
                fields=unique_fields,
                parsed=parsed.model_copy(update={"context": context}),
            )
        return parsed.model_copy(update={"context": context})

    async def _resolve_location(self, context: ReportContext) -> ReportContext:
        if context.lat is not None and context.lon is not None:
            return context.model_copy(
                update={
                    "geo_confidence": GeoConfidence.EXACT,
                    "location_source": "reported",
                }
            )
        location_text = (context.location_text or "").strip()
        if not location_text:
            return context.model_copy(update={"geo_confidence": GeoConfidence.NONE})
        result = await self._geocoder.geocode(self._incident_id, location_text)
        if result.lat is None or result.lon is None or result.confidence is GeoConfidence.NONE:
            return context.model_copy(update={"geo_confidence": GeoConfidence.NONE})
        return context.model_copy(
            update={
                "lat": result.lat,
                "lon": result.lon,
                "geo_confidence": result.confidence,
                "location_source": result.source,
            }
        )


@dataclass(frozen=True, slots=True)
class PersistedReports:
    entity_id: UUID | None
    report_ids: tuple[UUID, ...]


class ReportPersistence:
    """Persist validated reports and their entity inside the caller's transaction.

    Reports are evidence, not commitments: no Need, Supply, or Allocation is
    created here. A report without coordinates is still persisted and remains
    list-visible; matching later excludes it because it cannot produce a
    distance.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def persist(
        self,
        *,
        incident_id: UUID,
        message_id: UUID,
        sender_id: UUID,
        parsed: ParsedEmail,
    ) -> PersistedReports:
        context = parsed.context
        if context.kind not in MATCHED_KINDS:
            return PersistedReports(entity_id=None, report_ids=())
        name = (context.entity_name or "").strip()
        if not name:
            return PersistedReports(entity_id=None, report_ids=())
        entity = await self._upsert_entity(incident_id, name, context)

        report_ids: list[UUID] = []
        for report in parsed.reports:
            row = Report(
                id=uuid4(),
                incident_id=incident_id,
                message_id=message_id,
                sender_id=sender_id,
                entity_id=entity.id,
                kind=context.kind,
                resource=report.resource,
                quantity=report.quantity,
                unit=report.unit,
                quantity_source=(
                    QuantitySource.STATED if report.quantity is not None else None
                ),
                qualifier=report.qualifier,
                raw_text=report.raw_text,
                entity_name=name,
                entity_type=context.entity_type,
                location_text=context.location_text,
                lat=context.lat,
                lon=context.lon,
                geo_confidence=context.geo_confidence,
                location_source=context.location_source,
                urgency=context.urgency,
                headcount=context.headcount,
                confidence=context.confidence.model_dump(),
            )
            self._session.add(row)
            report_ids.append(row.id)
        await self._session.flush()
        return PersistedReports(entity_id=entity.id, report_ids=tuple(report_ids))

    async def _upsert_entity(
        self, incident_id: UUID, name: str, context: ReportContext
    ) -> Entity:
        normalized = normalize_entity_name(name)
        entity = await self._session.scalar(
            sa.select(Entity).where(
                Entity.incident_id == incident_id,
                Entity.normalized_name == normalized,
            )
        )
        if entity is None:
            entity = Entity(
                id=uuid4(),
                incident_id=incident_id,
                name=name,
                normalized_name=normalized,
                entity_type=context.entity_type,
                location_text=context.location_text,
                lat=context.lat,
                lon=context.lon,
                geo_confidence=context.geo_confidence,
                location_source=context.location_source,
            )
            try:
                async with self._session.begin_nested():
                    self._session.add(entity)
                    await self._session.flush()
            except IntegrityError:
                raced = await self._session.scalar(
                    sa.select(Entity).where(
                        Entity.incident_id == incident_id,
                        Entity.normalized_name == normalized,
                    )
                )
                if raced is None:  # pragma: no cover - constraint without a winner
                    raise
                entity = raced
            return entity

        # Later authoritative reports may add a location or a more specific
        # type, but never erase what is already known.
        if entity.lat is None and context.lat is not None:
            entity.lat = context.lat
            entity.lon = context.lon
            entity.geo_confidence = context.geo_confidence
            entity.location_source = context.location_source
        if entity.entity_type is EntityType.OTHER and context.entity_type is not EntityType.OTHER:
            entity.entity_type = context.entity_type
        if not entity.location_text and context.location_text:
            entity.location_text = context.location_text
        return entity


def persisted_reports_payload(result: PersistedReports, parsed: ParsedEmail) -> dict[str, Any]:
    """A PII-free audit payload for a persisted report batch."""
    return {
        "entity_id": str(result.entity_id) if result.entity_id else None,
        "report_ids": [str(report_id) for report_id in result.report_ids],
        "resources": [report.resource.value for report in parsed.reports],
    }


__all__ = [
    "ITEMS_CONFIDENCE_MIN",
    "LOCATION_CONFIDENCE_MIN",
    "MATCHED_KINDS",
    "MAX_PLAUSIBLE_QUANTITY",
    "ExtractionService",
    "InjectionFinding",
    "PersistedReports",
    "ReportPersistence",
    "detect_injection",
    "normalize_entity_name",
    "persisted_reports_payload",
]

"""Extraction orchestration: validate, geocode, and request one clarification.

The model output is already schema-validated by the adapter. This service adds
the deterministic gates that stop low-confidence or unlocatable data from
reaching matching.
"""

from __future__ import annotations

from uuid import UUID

from ..ports import Geocoder, IntelligenceProvider, RawMessage
from ..schemas import (
    ClarificationRequired,
    GeoConfidence,
    ParsedEmail,
    ReportContext,
    ReportKind,
)

LOCATION_CONFIDENCE_MIN = 0.60
ITEMS_CONFIDENCE_MIN = 0.75
MATCHED_KINDS = frozenset({ReportKind.NEED, ReportKind.SUPPLY})


class ExtractionService:
    def __init__(
        self,
        incident_id: UUID,
        intelligence: IntelligenceProvider,
        geocoder: Geocoder,
    ) -> None:
        self._incident_id = incident_id
        self._intelligence = intelligence
        self._geocoder = geocoder

    async def parse(self, raw: RawMessage) -> ParsedEmail | ClarificationRequired:
        parsed = await self._intelligence.extract(raw)
        context = parsed.context
        fields: list[str] = list(parsed.needs_clarification)
        fields.extend(context.clarification_fields)

        if context.kind in MATCHED_KINDS and not (context.entity_name or "").strip():
            fields.append("entity_name")
        if context.confidence.location < LOCATION_CONFIDENCE_MIN:
            fields.append("location")
        if context.confidence.items < ITEMS_CONFIDENCE_MIN:
            fields.append("items")

        if context.kind in MATCHED_KINDS:
            context = await self._resolve_location(context)
            if context.geo_confidence is GeoConfidence.NONE:
                fields.append("location")

        unique_fields = list(dict.fromkeys(fields))
        if unique_fields:
            return ClarificationRequired(kind=context.kind, fields=unique_fields)
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

"""Strict domain schemas shared by adapters, services, and the API.

Email content is untrusted. Every value that survives extraction is validated
here before it can influence matching, stock, or public output.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Resource(StrEnum):
    POTABLE_WATER = "potable_water"
    FOOD = "food"
    BLANKETS = "blankets"
    MEDICINE = "medicine"
    FIRST_AID = "first_aid"
    SANITATION = "sanitation"
    SHELTER_SPACE = "shelter_space"
    POWER_CHARGING = "power_charging"
    FUEL = "fuel"
    TRANSPORT = "transport"
    VOLUNTEERS = "volunteers"
    OTHER = "other"


class ReportKind(StrEnum):
    NEED = "NEED"
    SUPPLY = "SUPPLY"
    STATUS_UPDATE = "STATUS_UPDATE"
    CANCEL = "CANCEL"
    QUERY = "QUERY"
    OTHER = "OTHER"


class EntityType(StrEnum):
    SHELTER = "shelter"
    DEPOT = "depot"
    HUB = "hub"
    OTHER = "other"


class Urgency(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"
    UNKNOWN = "unknown"


class GeoConfidence(StrEnum):
    EXACT = "exact"
    AREA = "area"
    NONE = "none"


class NeedStatus(StrEnum):
    OPEN = "open"
    PARTIAL = "partial"
    MATCHED = "matched"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"


class AllocationStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class FieldConfidence(StrictModel):
    location: float = Field(ge=0, le=1)
    items: float = Field(ge=0, le=1)
    headcount: float = Field(ge=0, le=1)


class ReportContext(StrictModel):
    kind: ReportKind
    entity_name: str | None = None
    entity_type: EntityType = EntityType.OTHER
    location_text: str | None = None
    lat: Decimal | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    lon: Decimal | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    geo_confidence: GeoConfidence = GeoConfidence.NONE
    location_source: str | None = None
    urgency: Urgency = Urgency.UNKNOWN
    headcount: int | None = Field(default=None, ge=0, le=100_000)
    reference_id: str | None = None
    clarification_fields: list[str] = Field(default_factory=list)
    confidence: FieldConfidence


class ParsedReport(StrictModel):
    resource: Resource
    quantity: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    unit: str | None = None
    qualifier: str | None = None
    raw_text: str | None = None


class ParsedEmail(StrictModel):
    context: ReportContext
    reports: list[ParsedReport] = Field(min_length=1)
    needs_clarification: list[str] = Field(default_factory=list)
    raw_excerpt: str | None = None


class ClarificationRequired(StrictModel):
    """One-round clarification outcome: the listed fields block matching."""

    fields: list[str] = Field(min_length=1)
    kind: ReportKind = ReportKind.OTHER
    reason: str | None = None


class CriticCandidate(StrictModel):
    allocation_ref: str
    need_ref: str
    supply_ref: str
    resource: Resource
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    unit: str
    distance_km: Decimal = Field(ge=0, allow_inf_nan=False)
    available_quantity: Decimal = Field(ge=0, allow_inf_nan=False)
    freshness_hours: Decimal = Field(ge=0, allow_inf_nan=False)


class CriticContext(StrictModel):
    incident_id: UUID
    need_ref: str
    entity_name: str | None = None
    location_text: str | None = None
    urgency: Urgency = Urgency.UNKNOWN
    is_medical: bool = False
    candidates: list[CriticCandidate] = Field(default_factory=list)


class CriticVerdict(StrictModel):
    verdict: Literal["approve", "revise", "reject"]
    objections: list[str] = Field(default_factory=list)
    suggested_change: str | None = None

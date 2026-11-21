"""Entity-aware duplicate-need decisions.

A duplicate requires strong entity evidence: the same entity, or a highly
similar normalized name together with compatible coordinates within 150 m.
Proximity alone is only a review candidate — two distinct shelters 100 m
apart are never merged. Cross-sender duplicates corroborate an existing
need; they never overwrite its authoritative quantity.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from enum import StrEnum
from uuid import UUID

from ..schemas import NeedStatus
from .matching import haversine_km

DUPLICATE_WINDOW = timedelta(hours=12)
NAME_SIMILARITY_THRESHOLD = 0.85
PROXIMITY_KM = Decimal("0.150")
OPEN_STATUSES = frozenset({NeedStatus.OPEN, NeedStatus.PARTIAL, NeedStatus.MATCHED})


class DuplicateKind(StrEnum):
    DUPLICATE = "duplicate"
    REVIEW_CANDIDATE = "review_candidate"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class DuplicateNeedView:
    need_id: UUID
    entity_id: UUID | None
    entity_name: str
    resource: str
    unit: str
    lat: Decimal | None
    lon: Decimal | None
    status: NeedStatus
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DuplicateDecision:
    kind: DuplicateKind
    existing_id: UUID | None = None
    similarity: float = 0.0
    distance_km: Decimal | None = None


def normalize_name(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def name_similarity(first: str, second: str) -> float:
    """Deterministic similarity of two entity names in ``[0, 1]``."""
    return SequenceMatcher(
        None, normalize_name(first), normalize_name(second), autojunk=False
    ).ratio()


def find_duplicate(
    incoming: DuplicateNeedView,
    existing: list[DuplicateNeedView],
    now: datetime,
) -> DuplicateDecision:
    """Classify ``incoming`` against open needs from the same incident.

    ``existing`` must already be restricted to the incoming incident; the
    strongest duplicate wins deterministically (same entity first, then
    highest name similarity, then earliest need).
    """
    duplicates: list[tuple[bool, float, DuplicateNeedView, Decimal | None]] = []
    candidates: list[tuple[DuplicateNeedView, float, Decimal]] = []
    for other in existing:
        if other.need_id == incoming.need_id:
            continue
        if (
            other.resource != incoming.resource
            or other.unit != incoming.unit
            or other.status not in OPEN_STATUSES
        ):
            continue
        if now - other.created_at >= DUPLICATE_WINDOW:
            continue
        distance: Decimal | None = None
        if (
            incoming.lat is not None
            and incoming.lon is not None
            and other.lat is not None
            and other.lon is not None
        ):
            distance = haversine_km(incoming.lat, incoming.lon, other.lat, other.lon)
        similarity = name_similarity(incoming.entity_name, other.entity_name)
        same_entity = (
            incoming.entity_id is not None and other.entity_id == incoming.entity_id
        )
        nearby = distance is not None and distance <= PROXIMITY_KM
        if same_entity or (similarity >= NAME_SIMILARITY_THRESHOLD and nearby):
            duplicates.append((same_entity, similarity, other, distance))
        elif distance is not None and distance <= PROXIMITY_KM:
            candidates.append((other, similarity, distance))

    if duplicates:
        duplicates.sort(
            key=lambda item: (
                not item[0],
                -item[1],
                item[2].created_at,
                str(item[2].need_id),
            )
        )
        _, similarity, match, distance = duplicates[0]
        return DuplicateDecision(
            kind=DuplicateKind.DUPLICATE,
            existing_id=match.need_id,
            similarity=similarity,
            distance_km=distance,
        )
    if candidates:
        candidates.sort(key=lambda item: (item[0].created_at, str(item[0].need_id)))
        match, similarity, distance = candidates[0]
        return DuplicateDecision(
            kind=DuplicateKind.REVIEW_CANDIDATE,
            existing_id=match.need_id,
            similarity=similarity,
            distance_km=distance,
        )
    return DuplicateDecision(kind=DuplicateKind.NONE)

"""Gazetteer-first geocoding with a durable, incident-scoped cache.

Resolution order is deliberate: a curated local gazetteer gives an ``exact``
location without network access, then one cached Nominatim lookup gives an
``area`` location, then ``none``. Timeouts and malformed provider responses are
not cached, so a later retry can still resolve them; a definitive empty result
is cached as a negative entry.

Replay mode constructs the adapter with ``enabled=False`` and never opens a
network connection.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import GeocodeCache
from ..ports import GeoResult
from ..schemas import GeoConfidence

logger = logging.getLogger("resilinet.adapters.nominatim")

GAZETTEER_SOURCE = "gazetteer"
NOMINATIM_SOURCE = "nominatim"


class GazetteerError(ValueError):
    """The gazetteer fixture is malformed."""


def normalize_place(value: str) -> str:
    """Normalise a place string for stable lookup and cache keys."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split()).strip(" ,.;:")


class Gazetteer:
    """A small, deterministic set of known places."""

    def __init__(self, places: Mapping[str, tuple[Decimal, Decimal]]) -> None:
        if not places:
            raise GazetteerError("a gazetteer needs at least one place")
        self._places: dict[str, tuple[Decimal, Decimal]] = {}
        for name, (lat, lon) in places.items():
            key = normalize_place(name)
            if not key:
                raise GazetteerError("a gazetteer place has an empty name")
            if not (Decimal("-90") <= lat <= Decimal("90")):
                raise GazetteerError(f"latitude out of range for {name!r}")
            if not (Decimal("-180") <= lon <= Decimal("180")):
                raise GazetteerError(f"longitude out of range for {name!r}")
            self._places[key] = (lat, lon)

    @classmethod
    def from_file(cls, path: Path) -> Gazetteer:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        return cls.from_payload(payload)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Gazetteer:
        entries = payload.get("places")
        if not isinstance(entries, list) or not entries:
            raise GazetteerError("gazetteer payload has no places list")
        places: dict[str, tuple[Decimal, Decimal]] = {}
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise GazetteerError("a gazetteer entry is not an object")
            name = entry.get("name")
            if not isinstance(name, str):
                raise GazetteerError("a gazetteer entry has no name")
            try:
                lat = Decimal(str(entry["lat"]))
                lon = Decimal(str(entry["lon"]))
            except (KeyError, InvalidOperation, ValueError) as exc:
                raise GazetteerError(f"gazetteer entry {name!r} has invalid coordinates") from exc
            places[name] = (lat, lon)
            aliases = entry.get("aliases", [])
            if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
                raise GazetteerError(f"gazetteer entry {name!r} has invalid aliases")
            for alias in aliases:
                places[alias] = (lat, lon)
        return cls(places)

    @property
    def size(self) -> int:
        return len(self._places)

    def lookup(self, location_text: str) -> GeoResult | None:
        normalized = normalize_place(location_text)
        if not normalized:
            return None
        matches = [
            (name, coordinates)
            for name, coordinates in self._places.items()
            if name in normalized
        ]
        if not matches:
            return None
        _, (lat, lon) = max(matches, key=lambda item: len(item[0]))
        return GeoResult(
            lat=lat,
            lon=lon,
            confidence=GeoConfidence.EXACT,
            source=GAZETTEER_SOURCE,
        )


class NominatimGeocoder:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        gazetteer: Gazetteer,
        base_url: str = "https://nominatim.openstreetmap.org",
        contact_email: str | None = None,
        timeout_seconds: float = 5.0,
        enabled: bool = True,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._gazetteer = gazetteer
        self._enabled = enabled
        self._contact = (contact_email or "").strip()
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 3.0)),
            headers={
                "User-Agent": self._user_agent(),
                "Accept": "application/json",
            },
        )

    def _user_agent(self) -> str:
        contact = f" ({self._contact})" if self._contact else ""
        return f"ResiliNet/0.1 relief-coordination geocoder{contact}"

    async def geocode(self, incident_id: UUID, location_text: str) -> GeoResult:
        normalized = normalize_place(location_text)
        if not normalized:
            return _none_result("none")

        # The curated gazetteer is authoritative and local: it must win over a
        # cached provider result (including a negative one), so a place added
        # later is never blocked by an earlier "unknown".
        gazetteer = self._gazetteer.lookup(location_text)
        if gazetteer is not None:
            return gazetteer

        cached = await self._read_cache(incident_id, normalized)
        if cached is not None:
            return cached

        if not self._enabled:
            return _none_result("replay")

        result = await self._query_nominatim(incident_id, location_text)
        if result is not None:
            await self._write_cache(incident_id, normalized, result)
        return result or _none_result(NOMINATIM_SOURCE)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _query_nominatim(
        self, incident_id: UUID, location_text: str
    ) -> GeoResult | None:
        try:
            response = await self._http.get(
                "/search",
                params={
                    "q": location_text,
                    "format": "jsonv2",
                    "limit": 1,
                    "addressdetails": 0,
                },
                headers={
                    "User-Agent": self._user_agent(),
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            # Transient: do not cache, so a later retry is allowed.
            logger.info("nominatim_unavailable", extra={"error_type": type(exc).__name__})
            return None
        except ValueError:
            logger.info("nominatim_malformed_response")
            return None
        if not isinstance(payload, list):
            logger.info("nominatim_malformed_response")
            return None
        if not payload:
            await self._write_cache(
                incident_id,
                normalize_place(location_text),
                _none_result(NOMINATIM_SOURCE),
            )
            return None
        item = payload[0]
        if not isinstance(item, Mapping):
            logger.info("nominatim_malformed_response")
            return None
        try:
            lat = Decimal(str(item["lat"]))
            lon = Decimal(str(item["lon"]))
        except (KeyError, InvalidOperation, ValueError):
            logger.info("nominatim_malformed_response")
            return None
        if not (Decimal("-90") <= lat <= Decimal("90")) or not (
            Decimal("-180") <= lon <= Decimal("180")
        ):
            logger.info("nominatim_malformed_response")
            return None
        return GeoResult(
            lat=lat,
            lon=lon,
            confidence=GeoConfidence.AREA,
            source=NOMINATIM_SOURCE,
        )

    async def _read_cache(self, incident_id: UUID, normalized: str) -> GeoResult | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                sa.select(GeocodeCache).where(
                    GeocodeCache.incident_id == incident_id,
                    GeocodeCache.normalized_location == normalized,
                )
            )
            if row is None:
                return None
            return GeoResult(
                lat=row.lat,
                lon=row.lon,
                confidence=row.confidence,
                source=row.source,
            )

    async def _write_cache(
        self, incident_id: UUID, normalized: str, result: GeoResult
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await _upsert_cache(session, incident_id, normalized, result)


async def _upsert_cache(
    session: AsyncSession,
    incident_id: UUID,
    normalized: str,
    result: GeoResult,
) -> None:
    row = await session.scalar(
        sa.select(GeocodeCache).where(
            GeocodeCache.incident_id == incident_id,
            GeocodeCache.normalized_location == normalized,
        )
    )
    if row is None:
        row = GeocodeCache(
            id=uuid4(),
            incident_id=incident_id,
            normalized_location=normalized,
            lat=result.lat,
            lon=result.lon,
            confidence=result.confidence,
            source=result.source,
        )
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError:
            row = await session.scalar(
                sa.select(GeocodeCache).where(
                    GeocodeCache.incident_id == incident_id,
                    GeocodeCache.normalized_location == normalized,
                )
            )
            if row is None:  # pragma: no cover - constraint without a winner
                raise
    row.lat = result.lat
    row.lon = result.lon
    row.confidence = result.confidence
    row.source = result.source


def _none_result(source: str) -> GeoResult:
    return GeoResult(lat=None, lon=None, confidence=GeoConfidence.NONE, source=source)


__all__ = [
    "GAZETTEER_SOURCE",
    "NOMINATIM_SOURCE",
    "Gazetteer",
    "GazetteerError",
    "NominatimGeocoder",
    "normalize_place",
]

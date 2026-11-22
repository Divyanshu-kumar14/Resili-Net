"""Seed the fictional demo incident from fixtures/demo.json (idempotent).

Examples:

    uv run python scripts/seed_demo.py
    uv run python scripts/seed_demo.py --database-url sqlite+aiosqlite:///./demo.db

Seeding never verifies a sender and never issues a token: memberships are
created unverified, exactly as with scripts/seed_memberships.py. The script
prints counts only. Run it twice to prove idempotency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.config import Settings
from resilinet.db import create_engine, create_session_factory, transaction
from resilinet.domain.duplicates import normalize_name
from resilinet.domain.quantities import QuantitySource
from resilinet.models import (
    Entity,
    Incident,
    Need,
    ParticipantRoleKind,
    Supply,
)
from resilinet.schemas import EntityType, NeedStatus, Resource, Urgency
from resilinet.services.memberships import ensure_membership

ROOT = Path(__file__).resolve().parents[1]
DEMO = json.loads((ROOT / "fixtures" / "demo.json").read_text(encoding="utf-8"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed the ResiliNet demo incident.")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--incident-key", default=None)
    return parser.parse_args(argv)


async def seed_demo(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    incident_key: str,
) -> dict[str, int]:
    """Create-or-reuse the demo incident, entities, stock, needs, memberships."""
    async with transaction(session_factory) as session:
        incident = await session.scalar(
            sa.select(Incident).where(Incident.key == incident_key)
        )
        if incident is None:
            incident = Incident(
                key=incident_key,
                name=DEMO["incident"]["name"],
                simulation=bool(DEMO["incident"]["simulation"]),
            )
            session.add(incident)
            await session.flush()

        entities: dict[str, Entity] = {}
        for spec in DEMO["entities"]:
            normalized = normalize_name(spec["name"])
            entity = await session.scalar(
                sa.select(Entity).where(
                    Entity.incident_id == incident.id,
                    Entity.normalized_name == normalized,
                )
            )
            if entity is None:
                entity = Entity(
                    incident_id=incident.id,
                    name=spec["name"],
                    normalized_name=normalized,
                    entity_type=EntityType(spec["type"]),
                    lat=Decimal(spec["lat"]),
                    lon=Decimal(spec["lon"]),
                    geo_confidence="exact",
                )
                session.add(entity)
                await session.flush()
            entities[spec["name"]] = entity

        for item in DEMO["stock"]:
            entity = entities[item["entity"]]
            resource = Resource(item["resource"])
            supply = await session.scalar(
                sa.select(Supply).where(
                    Supply.incident_id == incident.id,
                    Supply.entity_id == entity.id,
                    Supply.resource == resource,
                )
            )
            if supply is None:
                session.add(
                    Supply(
                        incident_id=incident.id,
                        entity_id=entity.id,
                        resource=resource,
                        on_hand=Decimal(item["quantity"]),
                        unit=item["unit"],
                    )
                )
            else:
                supply.on_hand = Decimal(item["quantity"])
                supply.unit = item["unit"]

        for item in DEMO["needs"]:
            entity = entities[item["entity"]]
            need = await session.scalar(
                sa.select(Need).where(
                    Need.incident_id == incident.id, Need.ref == item["ref"]
                )
            )
            if need is None:
                session.add(
                    Need(
                        incident_id=incident.id,
                        entity_id=entity.id,
                        ref=item["ref"],
                        resource=Resource(item["resource"]),
                        quantity=Decimal(item["quantity"]),
                        unit=item["unit"],
                        quantity_source=QuantitySource(item["source"]),
                        urgency=Urgency.NORMAL,
                        status=NeedStatus(item["status"]),
                    )
                )

        for member in DEMO["memberships"]:
            await ensure_membership(
                session,
                incident_id=incident.id,
                address=member["address"],
                role=ParticipantRoleKind(member["role"]),
                entity_name=member.get("entity"),
            )

    async with session_factory() as session:
        return {
            "entities": int(
                await session.scalar(
                    sa.select(sa.func.count(Entity.id)).where(
                        Entity.incident_id == incident.id
                    )
                )
                or 0
            ),
            "stock": int(
                await session.scalar(
                    sa.select(sa.func.count(Supply.id)).where(
                        Supply.incident_id == incident.id
                    )
                )
                or 0
            ),
            "needs": int(
                await session.scalar(
                    sa.select(sa.func.count(Need.id)).where(
                        Need.incident_id == incident.id
                    )
                )
                or 0
            ),
        }


async def _run(settings: Settings) -> dict[str, int]:
    engine = create_engine(settings)
    try:
        return await seed_demo(
            create_session_factory(engine), incident_key=settings.incident_key
        )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base = Settings(_env_file=None)
    settings = base.model_copy(
        update={
            "database_url": args.database_url or base.database_url,
            "incident_key": args.incident_key or base.incident_key,
        }
    )
    try:
        counts = asyncio.run(_run(settings))
    except sa.exc.OperationalError:
        print(
            "database is not migrated; run `uv run alembic upgrade head` first",
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"incident_key": settings.incident_key, **counts}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

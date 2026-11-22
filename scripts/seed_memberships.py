"""Seed incident-scoped operators and coordinators without inventing verification.

Examples:

    uv run python scripts/seed_memberships.py \
        --operator ops@example.org \
        --depot-coordinator depot@example.org="Depot B" \
        --entity-coordinator shelter@example.org="Shelter 4"

The sender still has to complete the MojoAuth verification flow before any
session or action page works. The script prints counts and role names only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import sqlalchemy as sa

from resilinet.config import Settings
from resilinet.db import create_engine, create_session_factory, transaction
from resilinet.models import Incident, ParticipantRoleKind
from resilinet.services.memberships import MembershipError, ensure_membership


def _pair(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("use email=Entity Name")
    address, entity = value.split("=", 1)
    if not address.strip() or not entity.strip():
        raise argparse.ArgumentTypeError("use email=Entity Name")
    return address.strip(), entity.strip()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed incident-scoped ResiliNet memberships."
    )
    parser.add_argument("--operator", action="append", default=[])
    parser.add_argument(
        "--depot-coordinator", action="append", type=_pair, default=[]
    )
    parser.add_argument(
        "--entity-coordinator", action="append", type=_pair, default=[]
    )
    return parser.parse_args(argv)


async def seed(
    settings: Settings,
    *,
    operators: list[str],
    depot_coordinators: list[tuple[str, str]],
    entity_coordinators: list[tuple[str, str]],
) -> dict[str, Any]:
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    counts = {"operator": 0, "depot_coordinator": 0, "entity_coordinator": 0}
    try:
        async with session_factory() as session:
            incident_id = await session.scalar(
                sa.select(Incident.id).where(Incident.key == settings.incident_key)
            )
        if incident_id is None:
            raise MembershipError(
                f"incident {settings.incident_key!r} is not provisioned"
            )
        async with transaction(session_factory) as session:
            for address in operators:
                await ensure_membership(
                    session,
                    incident_id=incident_id,
                    address=address,
                    role=ParticipantRoleKind.OPERATOR,
                )
                counts["operator"] += 1
            for address, entity in depot_coordinators:
                await ensure_membership(
                    session,
                    incident_id=incident_id,
                    address=address,
                    role=ParticipantRoleKind.DEPOT_COORDINATOR,
                    entity_name=entity,
                )
                counts["depot_coordinator"] += 1
            for address, entity in entity_coordinators:
                await ensure_membership(
                    session,
                    incident_id=incident_id,
                    address=address,
                    role=ParticipantRoleKind.ENTITY_COORDINATOR,
                    entity_name=entity,
                )
                counts["entity_coordinator"] += 1
        return {
            "incident_key": settings.incident_key,
            "seeded": counts,
            "next": (
                "Each sender must still complete the MojoAuth verification flow "
                "before a session or action page works."
            ),
        }
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings()
    try:
        result = asyncio.run(
            seed(
                settings,
                operators=list(args.operator),
                depot_coordinators=list(args.depot_coordinator),
                entity_coordinators=list(args.entity_coordinator),
            )
        )
    except MembershipError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

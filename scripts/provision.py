"""Provision the configured incident and its Agentboxd inbox.

Run this once per environment after migrations:

    uv run alembic upgrade head
    uv run python scripts/provision.py

The script is idempotent: the incident is keyed by ``RESILINET_INCIDENT_KEY``
and Agentboxd's inbox creation is idempotent on the derived ``client_id``. It
prints the webhook URL to register, but never prints a credential.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import ProgrammingError

from resilinet.composition import build_inbox_provider
from resilinet.config import Settings
from resilinet.db import create_engine, create_session_factory
from resilinet.models import Incident


async def provision(
    settings: Settings,
    *,
    name: str,
    simulation: bool,
) -> dict[str, Any]:
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session, session.begin():
            incident = await session.scalar(
                sa.select(Incident).where(Incident.key == settings.incident_key)
            )
            if incident is None:
                incident = Incident(
                    key=settings.incident_key,
                    name=name,
                    simulation=simulation,
                )
                session.add(incident)
                await session.flush()
            elif simulation:
                incident.simulation = True
            incident_id = str(incident.id)
            simulation_value = incident.simulation

        inbox = build_inbox_provider(settings)
        try:
            mailbox = await inbox.provision_inbox(settings.incident_key)
        finally:
            await inbox.aclose()

        return {
            "incident_id": incident_id,
            "incident_key": settings.incident_key,
            "simulation": simulation_value,
            "mailbox_id": mailbox.id,
            "mailbox_address": mailbox.address,
            "webhook_url": settings.public_url("/internal/agentboxd/events"),
            "next": (
                "Register webhook_url for message.received in the Agentboxd "
                "dashboard, then copy its signing secret into "
                "RESILINET_AGENTBOXD_WEBHOOK_SECRET."
            ),
        }
    finally:
        await engine.dispose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Provision the configured ResiliNet incident and inbox."
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Human-readable incident name (default: the incident key).",
    )
    parser.add_argument(
        "--simulation",
        action="store_true",
        help="Mark the incident as a simulation for public projections.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings()
    if not settings.is_live:
        print(
            "RESILINET_APP_ENV must be live to provision the Agentboxd inbox",
            file=sys.stderr,
        )
        return 2
    try:
        result = asyncio.run(
            provision(
                settings,
                name=args.name or settings.incident_key,
                simulation=args.simulation,
            )
        )
    except ProgrammingError:
        print(
            "database schema is missing; run `uv run alembic upgrade head` first",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

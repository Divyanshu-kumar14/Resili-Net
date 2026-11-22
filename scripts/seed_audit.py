"""Seed a throwaway audit database for scripts/verify_accessibility.mjs.

Creates the demo incident, a verified operator and depot coordinator, one
open review case, one supply action link, and two test-only sessions, then
prints a JSON object with the cookies and paths the audit needs. The
database is a local throwaway file; tokens below are audit fixtures, not
credentials. Never run this against a deployed database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from datetime import timedelta
from pathlib import Path

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from seed_demo import seed_demo  # noqa: E402

from resilinet.config import Settings  # noqa: E402
from resilinet.db import create_engine, create_session_factory, transaction, utcnow  # noqa: E402
from resilinet.models import (  # noqa: E402
    AuthSession,
    Entity,
    Incident,
    Need,
    ParticipantRoleKind,
    PendingAction,
    PendingActionKind,
    ReviewCaseKind,
    Sender,
)
from resilinet.security import hash_token  # noqa: E402
from resilinet.services.actions import create_action_grant, payload_hash  # noqa: E402
from resilinet.services.memberships import ensure_membership  # noqa: E402
from resilinet.services.reviews import ReviewService  # noqa: E402

OPERATOR = "operator@example.org"
COORDINATOR = "depot-coordinator@example.org"


async def _verified_sender(session, incident_id, address, role, entity_name=None):
    _role, sender = await ensure_membership(
        session,
        incident_id=incident_id,
        address=address,
        role=role,
        entity_name=entity_name,
    )
    sender = await session.get(Sender, sender.id)
    assert sender is not None
    sender.verified = True
    sender.verified_at = utcnow()
    return sender


async def seed_audit(database_url: str) -> dict:
    settings = Settings(
        app_env="replay", database_url=database_url, _env_file=None
    )
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        await seed_demo(factory, incident_key=settings.incident_key)
        now = utcnow()
        async with transaction(factory) as session:
            incident = await session.scalar(
                sa.select(Incident).where(Incident.key == settings.incident_key)
            )
            assert incident is not None
            operator = await _verified_sender(
                session, incident.id, OPERATOR, ParticipantRoleKind.OPERATOR
            )
            coordinator = await _verified_sender(
                session,
                incident.id,
                COORDINATOR,
                ParticipantRoleKind.DEPOT_COORDINATOR,
                entity_name="Central Depot",
            )
            need = await session.scalar(
                sa.select(Need).where(
                    Need.incident_id == incident.id, Need.ref == "RN-000001"
                )
            )
            depot = await session.scalar(
                sa.select(Entity).where(
                    Entity.incident_id == incident.id, Entity.name == "Central Depot"
                )
            )
            assert need is not None and depot is not None
            await ReviewService(session).escalate(
                incident_id=incident.id,
                kind=ReviewCaseKind.MEDICAL,
                reason="audit fixture: medical report awaiting review",
                need_id=need.id,
            )
            payload = {
                "entity_id": str(depot.id),
                "resource": "potable_water",
                "quantity": "450",
                "unit": "l",
                "report_id": None,
            }
            pending = PendingAction(
                kind=PendingActionKind.UPDATE_SUPPLY,
                incident_id=incident.id,
                sender_id=coordinator.id,
                entity_id=depot.id,
                payload_json=payload,
                payload_hash=payload_hash(payload),
            )
            session.add(pending)
            await session.flush()
            _grant, token = await create_action_grant(
                session,
                kind=PendingActionKind.UPDATE_SUPPLY,
                incident_id=incident.id,
                sender_id=coordinator.id,
                entity_id=depot.id,
                pending_action_id=pending.id,
                payload_hash=pending.payload_hash,
                now=now,
            )
            cookies = {}
            for label, sender in (("operator", operator), ("coordinator", coordinator)):
                # Unique per run so re-seeding a database cannot collide
                # with the token-hash uniqueness constraint.
                raw = f"audit-{label}-session-{secrets.token_hex(6)}"
                session.add(
                    AuthSession(
                        incident_id=incident.id,
                        sender_id=sender.id,
                        token_hash=hash_token(raw),
                        expires_at=now + timedelta(hours=8),
                    )
                )
                cookies[label] = raw
        return {
            "operator_cookie": cookies["operator"],
            "coordinator_cookie": cookies["coordinator"],
            "session_cookie_name": "resilinet_session",
            "action_path": f"/actions/{token}",
            "reviews_path": "/operator/reviews",
        }
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the accessibility audit DB.")
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(asyncio.run(seed_audit(args.database_url))))
    return 0


if __name__ == "__main__":
    sys.exit(main())

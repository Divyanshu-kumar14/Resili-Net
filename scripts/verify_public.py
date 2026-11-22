"""Verify the public projection, cache behaviour, privacy, and lite budget.

Examples:

    uv run python scripts/verify_public.py
    uv run python scripts/verify_public.py --database-url sqlite+aiosqlite:///./resilinet.db

The script never calls an external host. It builds the ASGI app in-process,
reads the configured local database, and exits non-zero on any failed check.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from typing import Any
from uuid import UUID

import httpx
import sqlalchemy as sa

from resilinet.config import Settings
from resilinet.db import create_engine, create_session_factory
from resilinet.main import Container, create_app
from resilinet.models import Incident
from resilinet.services.public import PublicService

EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
EXTERNAL_URL = re.compile(r"https?://", re.IGNORECASE)
LOCAL_ASSET = re.compile(r'(?:href|src)="(/static/[^"]+)"')
FORBIDDEN_KEY_PARTS = (
    "address",
    "body_text",
    "email",
    "phone",
    "provider",
    "raw_text",
    "sender",
    "session",
    "token",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify ResiliNet public JSON, privacy, and lite budget."
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--incident-key", default=None)
    return parser.parse_args(argv)


def scan_privacy(node: object, path: str = "$") -> list[str]:
    problems: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = key.casefold()
            for part in FORBIDDEN_KEY_PARTS:
                if part in lowered:
                    problems.append(f"{path}.{key}: forbidden key")
            problems.extend(scan_privacy(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            problems.extend(scan_privacy(value, f"{path}[{index}]"))
    elif isinstance(node, str):
        if EMAIL.search(node):
            problems.append(f"{path}: email-like value")
        if "token" in node.casefold():
            problems.append(f"{path}: token-like value")
    return problems


async def verify(settings: Settings) -> tuple[dict[str, Any], list[str]]:
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    findings: list[str] = []
    report: dict[str, Any] = {"incident_key": settings.incident_key}
    try:
        try:
            async with session_factory() as session:
                incident = await session.scalar(
                    sa.select(Incident).where(Incident.key == settings.incident_key)
                )
        except sa.exc.OperationalError:
            return report, [
                "database is not migrated or does not exist; run "
                "`uv run alembic upgrade head` first"
            ]
        if incident is None:
            return report, [
                f"incident {settings.incident_key!r} is not provisioned"
            ]
        incident_id: UUID = incident.id
        snapshot = await PublicService(session_factory).map(incident_id)

        report["incident_id"] = str(incident_id)
        if not hasattr(snapshot, "body"):
            findings.append("map snapshot unexpectedly returned NotModified")
        else:
            report["counts"] = {
                "entities": len(snapshot.body["entities"]),
                "needs": len(snapshot.body["needs"]),
                "supplies": len(snapshot.body["supplies"]),
                "allocations": len(snapshot.body["allocations"]),
            }
            findings.extend(scan_privacy(snapshot.body))

        app = create_app(
            settings,
            Container(settings=settings, session_factory=session_factory),
        )
        transport = httpx.ASGITransport(app=app)
        base = f"/api/v1/incidents/{incident_id}"
        async with httpx.AsyncClient(
            transport=transport, base_url="http://verify.local"
        ) as client:
            public_map = await client.get(f"{base}/map")
            needs = await client.get(f"{base}/needs")
            summary = await client.get(f"{base}/summary")
            replay = await client.get("/replay", params={"incident_id": str(incident_id)})
            lite = await client.get(
                "/replay/lite", params={"incident_id": str(incident_id)}
            )
            assets = []
            for asset_path in LOCAL_ASSET.findall(lite.text):
                asset = await client.get(asset_path)
                if asset.status_code != 200:
                    findings.append(f"lite asset {asset_path} returned {asset.status_code}")
                assets.append(asset.content)

            for name, response in (
                ("map", public_map),
                ("needs", needs),
                ("summary", summary),
            ):
                if response.status_code != 200:
                    findings.append(f"{name} returned {response.status_code}")
                    continue
                findings.extend(scan_privacy(response.json(), f"$.{name}"))
                if response.headers.get("cache-control") != "no-cache":
                    findings.append(f"{name} missing Cache-Control: no-cache")
                if not response.headers.get("etag"):
                    findings.append(f"{name} missing ETag")

            etag = public_map.headers.get("etag")
            report["etag"] = etag
            if etag:
                revalidated = await client.get(
                    f"{base}/map", headers={"If-None-Match": etag}
                )
                report["revalidation_status"] = revalidated.status_code
                if revalidated.status_code != 304:
                    findings.append(
                        f"If-None-Match returned {revalidated.status_code}, expected 304"
                    )
                if revalidated.content:
                    findings.append("304 response carried a body")

            for name, response in (("replay", replay), ("lite", lite)):
                if response.status_code != 200:
                    findings.append(f"/{name} returned {response.status_code}")
                    continue
                if EXTERNAL_URL.search(response.text):
                    findings.append(f"/{name} references an external URL")
                findings.extend(scan_privacy(response.text, f"$.{name}_html"))

            report["views"] = {
                "replay_bytes": len(replay.content),
                "lite_bytes": len(lite.content),
                "lite_assets_bytes": sum(len(asset) for asset in assets),
                "lite_transferred": len(lite.content)
                + sum(len(asset) for asset in assets),
            }
            if "<script" in lite.text.casefold():
                findings.append("lite view contains a script element")
            if "<svg" in lite.text.casefold():
                findings.append("lite view contains an SVG map")
            if report["views"]["lite_transferred"] >= 100_000:
                findings.append("lite transfer is not under 100 KB")
    finally:
        await engine.dispose()
    return report, findings


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base = Settings(_env_file=None)
    settings = base.model_copy(
        update={
            "app_env": "test",
            "database_url": args.database_url or base.database_url,
            "incident_key": args.incident_key or base.incident_key,
        }
    )
    report, findings = asyncio.run(verify(settings))
    report["ok"] = not findings
    report["findings"] = findings
    print(json.dumps(report, indent=2, default=str))
    if findings:
        print("public verification failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

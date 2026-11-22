"""Live smoke test for the deployed ResiliNet service (Phase 6, Task 26).

The script never falls back to fake providers: it refuses to run unless
``RESILINET_APP_ENV=live`` and every live secret is present. With no owner
credentials it exits 2 after printing a redacted preflight report that names
the exact blocker.

Automated checks (all read-only except one idempotent inbox provisioning):

1. preflight: live env, required secrets, PostgreSQL URL, https public base,
   explicit test recipient;
2. ``GET /api/v1/health`` and ``GET /api/v1/ready`` against the deployed host;
3. the configured incident exists and is flagged ``simulation`` (the script
   aborts rather than touching a real incident);
4. the public map/needs/summary projections contain no sender, token,
   session, or provider-payload fields;
5. optionally (``--expect-text``), poll the public map until a manually sent
   probe email appears, measuring reply-to-map latency.

The inbound probe itself is manual on purpose: the owner sends one short
email from the test recipient to the printed inbox address, then the script
measures how fast it becomes visible. Nothing here sends mail as anyone.

Examples:

    uv run python scripts/smoke_live.py --mode live --recipient tester@example.org
    uv run python scripts/smoke_live.py --mode live --recipient tester@example.org \\
        --expect-text "smoke probe 7f3a" --wait-seconds 180
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from typing import Any

import httpx
import sqlalchemy as sa
from pydantic import ValidationError

from resilinet.composition import build_inbox_provider
from resilinet.config import Settings
from resilinet.db import create_engine, create_session_factory
from resilinet.models import Incident

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

EMAIL_LIKE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test the live ResiliNet deployment.")
    parser.add_argument("--mode", default="live", choices=("live",))
    parser.add_argument("--recipient", default=None, help="Test mailbox that sends the probe email.")
    parser.add_argument("--expect-text", default=None, help="Probe marker to poll for on the public map (use a random non-PII token; it is matched locally and never printed).")
    parser.add_argument("--wait-seconds", type=int, default=180)
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args(argv)


def redact_recipient(recipient: str) -> str:
    local, _, domain = recipient.partition("@")
    if not domain:
        return "***"
    return f"***@{domain}" if local else "***"


def preflight(settings: Settings, recipient: str | None) -> list[str]:
    """Return blocker descriptions; empty means the live run may proceed."""
    errors: list[str] = []
    if not settings.is_live:
        errors.append(
            "refusing to run: RESILINET_APP_ENV is "
            f"{settings.app_env!r}, not 'live'; smoke_live never uses fake providers"
        )
        return errors
    required = [
        "agentboxd_api_key",
        "agentboxd_webhook_secret",
        "mojoauth_api_key",
        "nominatim_contact_email",
        "nvidia_api_key" if settings.llm_provider == "nvidia" else "openai_api_key",
    ]
    missing = [name for name in required if not settings.secret_value(name)]
    if missing:
        errors.append("missing live settings: " + ", ".join(missing))
    scheme = settings.database_url.strip().lower()
    if "postgres" not in scheme:
        errors.append("live smoke requires a PostgreSQL DATABASE_URL")
    if not settings.public_base_url.startswith("https://"):
        errors.append("live smoke requires an https RESILINET_PUBLIC_BASE_URL")
    if not recipient or "@" not in recipient:
        errors.append("a test --recipient mailbox is required (owner-approved sender)")
    if not settings.momen_origins:
        errors.append(
            "no RESILINET_MOMEN_ORIGINS configured: without the published "
            "Momen origin the browser map cannot update, so the headline "
            "reply-to-map latency is unmeasurable"
        )
    return errors


def scan_privacy(node: object, path: str = "$") -> list[str]:
    problems: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).casefold()
            if any(part in lowered for part in FORBIDDEN_KEY_PARTS):
                problems.append(f"{path}.{key}")
            problems.extend(scan_privacy(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            problems.extend(scan_privacy(value, f"{path}[{index}]"))
    elif isinstance(node, str):
        # Findings record the path and kind only, never the value itself, so
        # the report cannot become a second copy of the leaked PII.
        if EMAIL_LIKE.search(node):
            problems.append(f"{path}: email-like value")
        if "token" in node.casefold():
            problems.append(f"{path}: token-like value")
    return problems


def _summarize_response(response: httpx.Response) -> dict[str, Any]:
    """Status plus privacy findings for an HTTP body, never the body itself."""
    try:
        body: object = response.json()
    except ValueError:
        body = response.text
    return {"status": response.status_code, "privacy_findings": scan_privacy(body)}


async def check_incident_simulation(settings: Settings) -> dict[str, Any]:
    engine = create_engine(settings)
    try:
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            incident = await session.scalar(
                sa.select(Incident).where(Incident.key == settings.incident_key)
            )
            if incident is None:
                return {"ok": False, "error": f"incident {settings.incident_key!r} not provisioned"}
            return {"ok": True, "simulation": incident.simulation}
    finally:
        await engine.dispose()


async def live_smoke(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    try:
        settings = Settings()
    except ValidationError as exc:
        # Field names only: values (which may contain a database password)
        # must never reach a traceback or a report.
        fields = sorted({str(err["loc"][0]) for err in exc.errors() if err.get("loc")})
        return 2, {
            "ok": False,
            "mode": args.mode,
            "preflight_blockers": [
                "invalid live settings in fields: " + ", ".join(fields)
            ],
            "error": (
                "live smoke blocked: resolve the preflight blockers with owner "
                "credentials/approvals; no fake fallback was used"
            ),
        }
    recipient = args.recipient
    report: dict[str, Any] = {
        "ok": False,
        "mode": args.mode,
        "app_env": settings.app_env,
        "recipient": redact_recipient(recipient) if recipient else None,
        "incident_key": settings.incident_key,
        "public_base_url": settings.public_base_url,
    }
    blockers = preflight(settings, recipient)
    report["preflight_blockers"] = blockers
    if blockers:
        report["error"] = (
            "live smoke blocked: resolve the preflight blockers with owner "
            "credentials/approvals; no fake fallback was used"
        )
        return 2, report
    if args.preflight_only:
        report["ok"] = True
        report["note"] = "preflight only; no network calls were made"
        return 0, report

    base = settings.public_base_url.rstrip("/")
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        health = await client.get(f"{base}/api/v1/health")
        ready_response = await client.get(f"{base}/api/v1/ready")
    report["health"] = _summarize_response(health)
    report["ready"] = _summarize_response(ready_response)
    if report["health"]["status"] != 200 or report["ready"]["status"] != 200:
        report["error"] = "deployed service is not healthy/ready; see status codes"
        return 1, report

    incident = await check_incident_simulation(settings)
    report["incident"] = incident
    if not incident.get("ok") or not incident.get("simulation"):
        report["error"] = "aborting: incident is missing or is not flagged simulation"
        return 2, report

    inbox = build_inbox_provider(settings)
    try:
        mailbox = await inbox.provision_inbox(settings.incident_key)
        report["inbox_address_hint"] = "***@" + mailbox.address.partition("@")[2]
    finally:
        await inbox.aclose()

    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        map_response = await client.get(f"{base}/api/v1/incidents/{settings.incident_key}/map")
        needs_response = await client.get(
            f"{base}/api/v1/incidents/{settings.incident_key}/needs"
        )
    map_body: Any = map_response.json()
    report["map_status"] = map_response.status_code
    report["needs_status"] = needs_response.status_code
    report["privacy_findings"] = scan_privacy(map_body) + scan_privacy(needs_response.json())
    if report["privacy_findings"]:
        report["error"] = "public projection leaks private fields"
        return 1, report

    if args.expect_text:
        marker = args.expect_text
        deadline = time.monotonic() + args.wait_seconds
        visible_at: float | None = None
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            while time.monotonic() < deadline:
                response = await client.get(
                    f"{base}/api/v1/incidents/{settings.incident_key}/map"
                )
                if marker in json.dumps(response.json()):
                    visible_at = time.monotonic()
                    break
                await asyncio.sleep(5)
        if visible_at is None:
            report["error"] = (
                "probe marker not visible within "
                f"{args.wait_seconds}s (marker value withheld from the report)"
            )
            return 1, report
        report["probe_visible_seconds"] = round(visible_at - started, 1)

    report["ok"] = True
    report["elapsed_seconds"] = round(time.monotonic() - started, 1)
    report["note"] = (
        "send one short probe email from the test recipient to the incident "
        "inbox to exercise inbound extraction; pass its marker as --expect-text "
        "to measure reply-to-map latency"
    )
    return 0, report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    exit_code, report = asyncio.run(live_smoke(args))
    print(json.dumps(report, indent=2, default=str))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

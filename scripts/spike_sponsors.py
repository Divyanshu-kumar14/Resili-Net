"""Owner-authorized sponsor capability spike.

Default behavior is read-only and never prints credentials. External side
effects (creating a persistent inbox, sending one magic link) require flags:

    uv run python scripts/spike_sponsors.py --offline
    uv run python scripts/spike_sponsors.py
    uv run python scripts/spike_sponsors.py --apply
    uv run python scripts/spike_sponsors.py --apply --send-magic-link \
        --test-email owner@example.org

Exit codes: 0 all checks ok, 1 a check failed, 2 a gate is blocked/unproven.
Momen is editor-only and always reported as blocked by this script.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from typing import Literal

import httpx

from resilinet.adapters.agentboxd import sign_webhook
from resilinet.config import Settings

Status = Literal["ok", "blocked", "failed", "skipped"]


@dataclass(frozen=True)
class Result:
    provider: str
    status: Status
    detail: str


def mask_email(value: str) -> str:
    local, separator, domain = value.partition("@")
    if not separator:
        return "***"
    return f"{local[:2]}***@{domain}"


def _config_presence(settings: Settings) -> list[Result]:
    results: list[Result] = []
    agentboxd = settings.secret_value("agentboxd_api_key")
    webhook = settings.secret_value("agentboxd_webhook_secret")
    if agentboxd and webhook:
        results.append(
            Result(
                "agentboxd",
                "blocked",
                "credentials present; run without --offline to verify the account",
            )
        )
    else:
        results.append(
            Result("agentboxd", "blocked", "missing Agentboxd API key or webhook secret")
        )

    mojoauth = settings.secret_value("mojoauth_api_key")
    results.append(
        Result(
            "mojoauth",
            "blocked",
            "optional until Phase 3; no identity adapter wired"
            if not mojoauth
            else "key present; callback encoding not verified offline",
        )
    )

    if settings.llm_provider == "nvidia":
        nvidia = settings.secret_value("nvidia_api_key")
        results.append(
            Result(
                "nvidia",
                "blocked",
                "missing NVIDIA NIM API key"
                if not nvidia
                else f"key present; model {settings.nvidia_model} not queried offline",
            )
        )
    else:
        openai = settings.secret_value("openai_api_key")
        results.append(
            Result(
                "openai",
                "blocked",
                "missing OpenAI API key"
                if not openai
                else f"key present; model {settings.openai_model} not queried offline",
            )
        )

    results.append(
        Result("momen", "blocked", "editor-only; use docs/momen-setup.md")
    )
    return results


async def check_agentboxd(
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    apply: bool,
) -> Result:
    api_key = settings.secret_value("agentboxd_api_key")
    webhook_secret = settings.secret_value("agentboxd_webhook_secret")
    if not api_key or not webhook_secret:
        return Result("agentboxd", "blocked", "missing API key or webhook secret")

    base = settings.agentboxd_base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    try:
        response = await client.get(f"{base}/v1/inboxes", params={"limit": 1}, headers=headers)
    except httpx.HTTPError as exc:
        return Result("agentboxd", "failed", f"network error: {type(exc).__name__}")

    if response.status_code in (401, 403):
        return Result("agentboxd", "failed", f"key rejected (HTTP {response.status_code})")
    if response.status_code != 200:
        return Result("agentboxd", "failed", f"listing inboxes returned HTTP {response.status_code}")

    # Verify the documented HMAC formula locally; never print the secret.
    sample = b'{"type":"webhook.test"}'
    signature = sign_webhook(sample, webhook_secret, 1_700_000_000)
    if len(signature) != 64:
        return Result("agentboxd", "failed", "local HMAC self-check failed")

    detail = "key can list inboxes; webhook secret present; local HMAC self-check ok"
    if not apply:
        detail += "; re-run with --apply to provision a persistent inbox"
        return Result("agentboxd", "ok", detail)

    try:
        created = await client.post(
            f"{base}/v1/inboxes",
            headers=headers,
            json={"client_id": "resilinet-spike"},
        )
    except httpx.HTTPError as exc:
        return Result("agentboxd", "failed", f"provisioning network error: {type(exc).__name__}")
    if created.status_code not in (200, 201):
        return Result(
            "agentboxd", "failed", f"provisioning returned HTTP {created.status_code}"
        )
    payload = created.json()
    inbox_id = payload.get("id", "unknown")
    address = payload.get("address", "unknown")
    return Result(
        "agentboxd",
        "ok",
        f"persistent inbox id={inbox_id} address={address}; {detail}",
    )


async def check_mojoauth(
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    apply: bool,
    test_email: str | None,
    send_magic_link: bool,
) -> Result:
    api_key = settings.secret_value("mojoauth_api_key")
    if not api_key:
        return Result("mojoauth", "blocked", "missing RESILINET_MOJOAUTH_API_KEY")
    if not send_magic_link:
        return Result(
            "mojoauth",
            "blocked",
            "no read-only probe exists; use --apply --send-magic-link --test-email",
        )
    if not apply:
        return Result("mojoauth", "blocked", "--send-magic-link requires --apply")
    if not test_email:
        return Result("mojoauth", "failed", "--send-magic-link requires --test-email")

    try:
        response = await client.post(
            f"{settings.mojoauth_base_url.rstrip('/')}/users/magiclink",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json={
                "email": test_email,
                "language": "en",
                "redirect_url": settings.public_url("/auth/callback"),
            },
        )
    except httpx.HTTPError as exc:
        return Result("mojoauth", "failed", f"network error: {type(exc).__name__}")

    if response.status_code in (401, 403):
        return Result("mojoauth", "failed", f"key rejected (HTTP {response.status_code})")
    if response.status_code != 200:
        return Result("mojoauth", "failed", f"magic link returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        return Result("mojoauth", "failed", "magic link response was not JSON")
    return Result(
        "mojoauth",
        "ok",
        (
            f"magic link requested for {mask_email(test_email)}; "
            f"state_id present={bool(payload.get('state_id'))}; "
            "the redirect parameter encoding still needs a real click"
        ),
    )


async def check_openai(settings: Settings, client: httpx.AsyncClient) -> Result:
    api_key = settings.secret_value("openai_api_key")
    if not api_key:
        return Result("openai", "blocked", "missing RESILINET_OPENAI_API_KEY")
    try:
        response = await client.get(
            f"{settings.openai_base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    except httpx.HTTPError as exc:
        return Result("openai", "failed", f"network error: {type(exc).__name__}")

    if response.status_code in (401, 403):
        return Result("openai", "failed", f"key rejected (HTTP {response.status_code})")
    if response.status_code != 200:
        return Result("openai", "failed", f"model listing returned HTTP {response.status_code}")
    try:
        models = {item.get("id") for item in response.json().get("data", [])}
    except (ValueError, AttributeError):
        return Result("openai", "failed", "model listing was not the expected JSON")
    if settings.openai_model not in models:
        return Result(
            "openai",
            "blocked",
            f"model {settings.openai_model} is not listed for this key",
        )
    return Result("openai", "ok", f"model {settings.openai_model} is available")


async def check_nvidia(settings: Settings, client: httpx.AsyncClient) -> Result:
    api_key = settings.secret_value("nvidia_api_key")
    if not api_key:
        return Result("nvidia", "blocked", "missing RESILINET_NVIDIA_API_KEY")
    try:
        response = await client.get(
            f"{settings.nvidia_base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    except httpx.HTTPError as exc:
        return Result("nvidia", "failed", f"network error: {type(exc).__name__}")

    if response.status_code in (401, 403):
        return Result("nvidia", "failed", f"key rejected (HTTP {response.status_code})")
    if response.status_code != 200:
        return Result(
            "nvidia", "failed", f"model listing returned HTTP {response.status_code}"
        )
    try:
        models = {item.get("id") for item in response.json().get("data", [])}
    except (ValueError, AttributeError):
        return Result("nvidia", "failed", "model listing was not the expected JSON")
    if settings.nvidia_model not in models:
        return Result(
            "nvidia",
            "blocked",
            f"model {settings.nvidia_model} is not listed for this key",
        )
    return Result("nvidia", "ok", f"model {settings.nvidia_model} is available")


async def run(args: argparse.Namespace) -> list[Result]:
    settings = Settings()
    if settings.app_env != "live":
        return [
            Result(
                "configuration",
                "blocked",
                f"RESILINET_APP_ENV={settings.app_env}; set live for the real spike",
            )
        ]
    if args.offline:
        return _config_presence(settings)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(settings.request_timeout_seconds)
    ) as client:
        agentboxd = await check_agentboxd(settings, client, apply=args.apply)
        if settings.secret_value("mojoauth_api_key"):
            mojoauth = await check_mojoauth(
                settings,
                client,
                apply=args.apply,
                test_email=args.test_email,
                send_magic_link=args.send_magic_link,
            )
        else:
            mojoauth = Result(
                "mojoauth", "blocked", "optional until Phase 3; no identity adapter wired"
            )
        intelligence = (
            await check_nvidia(settings, client)
            if settings.llm_provider == "nvidia"
            else await check_openai(settings, client)
        )
        return [
            agentboxd,
            mojoauth,
            intelligence,
            Result("momen", "blocked", "editor-only; use docs/momen-setup.md"),
        ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify sponsor account capabilities without printing secrets."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform external side effects: create a persistent Agentboxd inbox",
    )
    parser.add_argument(
        "--send-magic-link",
        action="store_true",
        help="send one MojoAuth magic link; requires --apply and --test-email",
    )
    parser.add_argument(
        "--test-email",
        help="owner-controlled address for the MojoAuth spike",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="report configured capability presence without network calls",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    results = asyncio.run(run(args))
    print(json.dumps([asdict(result) for result in results], indent=2))
    if any(result.status == "failed" for result in results):
        return 1
    if any(result.status in ("blocked", "skipped") for result in results):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

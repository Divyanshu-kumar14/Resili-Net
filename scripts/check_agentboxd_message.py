"""Print delivery metadata for one Agentboxd message without its body.

Usage:
    uv run python scripts/check_agentboxd_message.py <message-id>

Reads ``RESILINET_AGENTBOXD_API_KEY`` and optionally
``RESILINET_AGENTBOXD_BASE_URL``. Never prints message text, HTML, headers,
attachments, or the API key.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

_PRINTED_KEYS = (
    "id",
    "status",
    "direction",
    "channel",
    "from",
    "to",
    "subject",
    "created_at",
    "sent_at",
    "delivered_at",
    "bounced_at",
    "failed_at",
    "error",
    "error_code",
    "labels",
    "screening",
)


async def check(message_id: str) -> int:
    api_key = os.environ.get("RESILINET_AGENTBOXD_API_KEY")
    if not api_key:
        print("missing RESILINET_AGENTBOXD_API_KEY", file=sys.stderr)
        return 2
    base = os.environ.get(
        "RESILINET_AGENTBOXD_BASE_URL", "https://api.agentboxd.com"
    ).rstrip("/")
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            f"{base}/v1/messages/{message_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    print("status_code", response.status_code)
    if response.status_code != 200:
        print(response.text[:1000])
        return 1
    payload = response.json()
    metadata = {key: payload[key] for key in _PRINTED_KEYS if key in payload}
    print(json.dumps(metadata, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(
            "usage: python scripts/check_agentboxd_message.py <message-id>",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(check(args[0]))


if __name__ == "__main__":
    sys.exit(main())

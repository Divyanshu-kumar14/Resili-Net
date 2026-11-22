"""MojoAuth magic-link adapter.

Documented contract (checked 2026-11-22 against the MojoAuth API reference):

* ``POST /users/magiclink`` with ``x-api-key`` and ``{email, language,
  redirect_url}`` returns ``{state_id, status}``.
* MojoAuth redirects to ``redirect_url`` with its ``state_id`` appended.
* ``GET /users/status?state_id=...`` returns ``authenticated`` and
  ``user.identifier`` (the verified email address).

The live account-level spike is still blocked, so this adapter is exercised
against recorded HTTP shapes with ``httpx.MockTransport`` only; no live result
is claimed. Provider payloads and error bodies never leave this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from ..ports import (
    AdapterError,
    ProviderUnavailableError,
    VerificationRequest,
    VerifiedIdentity,
)


class MojoAuthAuthProvider:
    """Magic-link verification through the documented MojoAuth HTTP API."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.mojoauth.com",
        timeout_seconds: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        key = api_key.strip()
        if not key:
            raise ValueError("MojoAuth requires a non-empty API key")
        self._api_key = key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key, "Accept": "application/json"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            if self._client is not None:
                response = await self._client.request(
                    method, f"{self._base_url}{path}", headers=self._headers(),
                    json=json, params=params,
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method, f"{self._base_url}{path}", headers=self._headers(),
                        json=json, params=params,
                    )
        except httpx.TimeoutException as exc:
            raise ProviderUnavailableError("MojoAuth request timed out") from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailableError("MojoAuth is unreachable") from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderUnavailableError(
                f"MojoAuth is unavailable (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            # Deliberately exclude the provider's error body: it can echo the
            # submitted address or token.
            raise AdapterError(f"MojoAuth rejected the request (HTTP {response.status_code})")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AdapterError("MojoAuth returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise AdapterError("MojoAuth returned an unexpected response shape")
        return payload

    async def send_magic_link(
        self, email: str, redirect_url: str
    ) -> VerificationRequest:
        if not email.strip():
            raise AdapterError("a magic link requires a recipient address")
        payload = await self._request(
            "POST",
            "/users/magiclink",
            json={"email": email, "language": "en", "redirect_url": redirect_url},
        )
        state_id = payload.get("state_id")
        if not isinstance(state_id, str) or not state_id.strip():
            raise AdapterError("MojoAuth response did not include a state id")
        status = payload.get("status")
        return VerificationRequest(
            state_id=state_id,
            status=status if isinstance(status, str) else "sent",
        )

    async def verify_callback(self, params: Mapping[str, str]) -> VerifiedIdentity:
        state_id = str(params.get("state_id", "")).strip()
        if not state_id:
            raise AdapterError("verification callback has no state id")
        payload = await self._request(
            "GET", "/users/status", params={"state_id": state_id}
        )
        authenticated = payload.get("authenticated")
        user = payload.get("user")
        identifier = user.get("identifier") if isinstance(user, dict) else None
        if not isinstance(identifier, str) or not identifier.strip():
            raise AdapterError("MojoAuth response did not include an identity")
        subject = user.get("user_id") if isinstance(user, dict) else None
        return VerifiedIdentity(
            email=identifier,
            verified=authenticated is True,
            provider_subject=subject if isinstance(subject, str) else None,
        )


__all__ = ["MojoAuthAuthProvider"]

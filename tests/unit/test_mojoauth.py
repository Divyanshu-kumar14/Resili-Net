"""MojoAuth wire-contract tests (recorded shapes; no live account)."""

from __future__ import annotations

import json

import httpx
import pytest

from resilinet.adapters.mojoauth import MojoAuthAuthProvider
from resilinet.ports import AdapterError, ProviderUnavailableError


def make_provider(handler: httpx.MockTransport) -> MojoAuthAuthProvider:
    client = httpx.AsyncClient(transport=handler)
    return MojoAuthAuthProvider(
        api_key="secret-key", base_url="https://api.test", client=client
    )


async def test_send_magic_link_uses_documented_request() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["api_key"] = request.headers.get("x-api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"state_id": "state-abc", "status": "success"})

    provider = make_provider(httpx.MockTransport(handler))
    result = await provider.send_magic_link(
        "person@example.org", "https://app.test/auth/callback?next=/"
    )

    assert result.state_id == "state-abc"
    assert result.status == "success"
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.test/users/magiclink"
    assert seen["api_key"] == "secret-key"
    assert seen["body"] == {
        "email": "person@example.org",
        "language": "en",
        "redirect_url": "https://app.test/auth/callback?next=/",
    }


async def test_verify_callback_reads_authenticated_identifier() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["api_key"] = request.headers.get("x-api-key")
        return httpx.Response(
            200,
            json={
                "authenticated": True,
                "user": {
                    "identifier": "person@example.org",
                    "user_id": "user-1",
                },
            },
        )

    provider = make_provider(httpx.MockTransport(handler))
    identity = await provider.verify_callback({"state_id": "state-abc"})

    assert identity.email == "person@example.org"
    assert identity.verified is True
    assert identity.provider_subject == "user-1"
    assert seen["method"] == "GET"
    assert seen["url"] == "https://api.test/users/status?state_id=state-abc"
    assert seen["api_key"] == "secret-key"


async def test_verify_callback_preserves_unauthenticated_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "authenticated": False,
                "user": {"identifier": "person@example.org"},
            },
        )

    identity = await make_provider(httpx.MockTransport(handler)).verify_callback(
        {"state_id": "state-abc"}
    )
    assert identity.verified is False


async def test_missing_state_is_rejected_without_a_network_call() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        nonlocal called
        called = True
        return httpx.Response(500)

    provider = make_provider(httpx.MockTransport(handler))
    with pytest.raises(AdapterError):
        await provider.verify_callback({})
    assert called is False


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_transient_failures_are_retryable(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="provider detail must not leak")

    provider = make_provider(httpx.MockTransport(handler))
    with pytest.raises(ProviderUnavailableError) as excinfo:
        await provider.send_magic_link("person@example.org", "https://app.test/cb")
    assert excinfo.value.retryable is True
    assert "provider detail" not in str(excinfo.value)


async def test_client_errors_do_not_echo_provider_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="person@example.org secret-key")

    provider = make_provider(httpx.MockTransport(handler))
    with pytest.raises(AdapterError) as excinfo:
        await provider.send_magic_link("person@example.org", "https://app.test/cb")
    assert "person@example.org" not in str(excinfo.value)
    assert "secret-key" not in str(excinfo.value)


async def test_malformed_payloads_are_adapter_errors() -> None:
    def not_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>")

    with pytest.raises(AdapterError):
        await make_provider(httpx.MockTransport(not_json)).send_magic_link(
            "person@example.org", "https://app.test/cb"
        )

    def no_state(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success"})

    with pytest.raises(AdapterError):
        await make_provider(httpx.MockTransport(no_state)).send_magic_link(
            "person@example.org", "https://app.test/cb"
        )

    def no_identity(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"authenticated": True, "user": {}})

    with pytest.raises(AdapterError):
        await make_provider(httpx.MockTransport(no_identity)).verify_callback(
            {"state_id": "state-abc"}
        )

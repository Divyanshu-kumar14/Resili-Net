"""Build runtime adapters from validated settings."""

from __future__ import annotations

import json
from pathlib import Path

from .adapters.agentboxd import AgentboxdInboxProvider
from .adapters.fakes import FakeInboxProvider, FakeIntelligenceProvider
from .adapters.mojoauth import MojoAuthAuthProvider
from .adapters.nvidia import NvidiaIntelligenceProvider
from .adapters.openai import OpenAIIntelligenceProvider
from .config import Settings
from .ports import AuthProvider, InboxProvider, IntelligenceProvider

RECORDED_EXTRACTS_PATH = (
    Path(__file__).resolve().parents[2] / "fixtures" / "replay" / "extracts.json"
)


def _replay_intelligence() -> FakeIntelligenceProvider:
    data = json.loads(RECORDED_EXTRACTS_PATH.read_text(encoding="utf-8"))
    return FakeIntelligenceProvider.from_recorded(data["extracts"])


def build_inbox_provider(settings: Settings) -> InboxProvider:
    """Build the Agentboxd inbox adapter used by the webhook and worker.

    Replay mode never constructs the live adapter, even if secrets exist.
    """
    if settings.is_replay:
        return FakeInboxProvider(webhook_secret="replay")

    api_key = settings.secret_value("agentboxd_api_key")
    webhook_secret = settings.secret_value("agentboxd_webhook_secret")
    if not api_key or not webhook_secret:
        raise RuntimeError("Agentboxd secrets are required in live mode")
    return AgentboxdInboxProvider(
        api_key=api_key,
        webhook_secret=webhook_secret,
        base_url=settings.agentboxd_base_url,
        timeout_seconds=settings.request_timeout_seconds,
    )


def build_auth_provider(settings: Settings) -> AuthProvider | None:
    """Return the MojoAuth adapter when an API key is configured.

    Test and replay modes deliberately run without it so no external call can
    occur; routes then serve only generic pages. Live mode fails startup
    earlier if the key is absent.
    """
    if settings.is_replay:
        return None
    api_key = settings.secret_value("mojoauth_api_key")
    if api_key is None:
        return None
    return MojoAuthAuthProvider(
        api_key=api_key,
        base_url=settings.mojoauth_base_url,
        timeout_seconds=settings.request_timeout_seconds,
    )


def build_intelligence_provider(settings: Settings) -> IntelligenceProvider:
    """Return the adapter selected by ``RESILINET_LLM_PROVIDER``.

    Live mode already guarantees the selected provider's key is present; the
    explicit checks keep test/replay misuse loud instead of constructing a
    provider that would fail on its first request. Replay mode always gets
    the recorded deterministic provider, even when live keys are present.
    """
    if settings.is_replay:
        return _replay_intelligence()

    if settings.llm_provider == "nvidia":
        api_key = settings.secret_value("nvidia_api_key")
        if api_key is None:
            raise RuntimeError(
                "nvidia provider selected without RESILINET_NVIDIA_API_KEY"
            )
        return NvidiaIntelligenceProvider(
            api_key=api_key,
            model=settings.nvidia_model,
            fallback_models=settings.nvidia_fallback_models,
            base_url=settings.nvidia_base_url,
            timeout_seconds=settings.request_timeout_seconds,
        )

    api_key = settings.secret_value("openai_api_key")
    if api_key is None:
        raise RuntimeError("openai provider selected without RESILINET_OPENAI_API_KEY")
    return OpenAIIntelligenceProvider(
        api_key=api_key,
        model=settings.openai_model,
        base_url=settings.openai_base_url,
        timeout_seconds=settings.request_timeout_seconds,
    )


__all__ = ["build_auth_provider", "build_inbox_provider", "build_intelligence_provider"]

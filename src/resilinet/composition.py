"""Build runtime adapters from validated settings."""

from __future__ import annotations

from .adapters.agentboxd import AgentboxdInboxProvider
from .adapters.nvidia import NvidiaIntelligenceProvider
from .adapters.openai import OpenAIIntelligenceProvider
from .config import Settings
from .ports import InboxProvider, IntelligenceProvider


def build_inbox_provider(settings: Settings) -> InboxProvider:
    """Build the Agentboxd inbox adapter used by the webhook and worker."""

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


def build_intelligence_provider(settings: Settings) -> IntelligenceProvider:
    """Return the adapter selected by ``RESILINET_LLM_PROVIDER``.

    Live mode already guarantees the selected provider's key is present; the
    explicit checks keep test/replay misuse loud instead of constructing a
    provider that would fail on its first request.
    """

    if settings.llm_provider == "nvidia":
        api_key = settings.secret_value("nvidia_api_key")
        if api_key is None:
            raise RuntimeError(
                "nvidia provider selected without RESILINET_NVIDIA_API_KEY"
            )
        return NvidiaIntelligenceProvider(
            api_key=api_key,
            model=settings.nvidia_model,
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


__all__ = ["build_inbox_provider", "build_intelligence_provider"]

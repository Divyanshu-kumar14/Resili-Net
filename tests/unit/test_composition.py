from __future__ import annotations

import pytest

from resilinet.adapters.agentboxd import AgentboxdInboxProvider
from resilinet.adapters.nvidia import NvidiaIntelligenceProvider
from resilinet.adapters.openai import OpenAIIntelligenceProvider
from resilinet.composition import build_inbox_provider, build_intelligence_provider
from resilinet.config import Settings


def test_factory_builds_openai_by_default() -> None:
    settings = Settings(openai_api_key="key", _env_file=None)

    assert isinstance(
        build_intelligence_provider(settings), OpenAIIntelligenceProvider
    )


def test_factory_builds_nvidia_when_selected() -> None:
    settings = Settings(
        llm_provider="nvidia", nvidia_api_key="key", _env_file=None
    )

    assert isinstance(
        build_intelligence_provider(settings), NvidiaIntelligenceProvider
    )


def test_factory_requires_the_selected_provider_key() -> None:
    with pytest.raises(RuntimeError, match="RESILINET_NVIDIA_API_KEY"):
        build_intelligence_provider(
            Settings(llm_provider="nvidia", _env_file=None)
        )


def test_factory_builds_agentboxd_inbox() -> None:
    settings = Settings(
        agentboxd_api_key="key",
        agentboxd_webhook_secret="secret",
        _env_file=None,
    )

    assert isinstance(build_inbox_provider(settings), AgentboxdInboxProvider)


def test_factory_requires_agentboxd_secrets() -> None:
    with pytest.raises(RuntimeError, match="Agentboxd"):
        build_inbox_provider(Settings(_env_file=None))

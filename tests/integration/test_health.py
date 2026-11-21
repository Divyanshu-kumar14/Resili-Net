from __future__ import annotations

import logging

import httpx
import pytest
from pydantic import ValidationError

from resilinet.config import Settings
from resilinet.main import create_app


async def test_health_returns_ok() -> None:
    app = create_app(Settings(app_env="test", _env_file=None), container=None)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        result = await client.get("/api/v1/health")

    assert result.status_code == 200
    assert result.json() == {"status": "ok"}


def test_live_configuration_requires_secrets() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            app_env="live",
            agentboxd_api_key=None,
            agentboxd_webhook_secret=None,
            mojoauth_api_key=None,
            openai_api_key=None,
            _env_file=None,
        )

    message = str(excinfo.value)
    assert "agentboxd_api_key" in message
    assert "openai_api_key" in message
    assert "mojoauth_api_key" not in message


def test_live_mode_does_not_require_mojoauth_until_identity_lands() -> None:
    settings = Settings(
        app_env="live",
        database_url="postgresql+asyncpg://user:pass@localhost/resilinet",
        agentboxd_api_key="key",
        agentboxd_webhook_secret="secret",
        mojoauth_api_key=None,
        openai_api_key="key",
        _env_file=None,
    )

    assert settings.is_live
    assert settings.secret_value("mojoauth_api_key") is None


def test_create_app_rejects_a_live_environment_without_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESILINET_APP_ENV", "live")
    monkeypatch.setenv("RESILINET_LLM_PROVIDER", "openai")
    for variable in (
        "RESILINET_AGENTBOXD_API_KEY",
        "RESILINET_AGENTBOXD_WEBHOOK_SECRET",
        "RESILINET_MOJOAUTH_API_KEY",
        "RESILINET_OPENAI_API_KEY",
        "RESILINET_NVIDIA_API_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)

    with pytest.raises(ValidationError):
        create_app()


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite+aiosqlite:///./local.db",
        "mysql+aiomysql://user:pass@localhost/resilinet",
        "",
    ],
)
def test_live_mode_rejects_non_postgresql(database_url: str) -> None:
    with pytest.raises(ValidationError, match="PostgreSQL"):
        Settings(
            app_env="live",
            database_url=database_url,
            agentboxd_api_key="key",
            agentboxd_webhook_secret="secret",
            mojoauth_api_key="key",
            openai_api_key="key",
            _env_file=None,
        )


def test_live_mode_accepts_a_postgresql_url() -> None:
    settings = Settings(
        app_env="live",
        database_url="postgresql+asyncpg://user:pass@localhost/resilinet",
        agentboxd_api_key="key",
        agentboxd_webhook_secret="secret",
        mojoauth_api_key="key",
        openai_api_key="key",
        _env_file=None,
    )

    assert settings.is_live


def test_live_mode_requires_the_selected_nvidia_key() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            app_env="live",
            llm_provider="nvidia",
            database_url="postgresql+asyncpg://user:pass@localhost/resilinet",
            agentboxd_api_key="key",
            agentboxd_webhook_secret="secret",
            mojoauth_api_key="key",
            openai_api_key="key",
            nvidia_api_key=None,
            _env_file=None,
        )

    message = str(excinfo.value)
    assert "nvidia_api_key" in message
    assert "openai_api_key" not in message


def test_live_mode_accepts_nvidia_without_an_openai_key() -> None:
    settings = Settings(
        app_env="live",
        llm_provider="nvidia",
        database_url="postgresql+asyncpg://user:pass@localhost/resilinet",
        agentboxd_api_key="key",
        agentboxd_webhook_secret="secret",
        mojoauth_api_key="key",
        openai_api_key=None,
        nvidia_api_key="nvidia-key",
        _env_file=None,
    )

    assert settings.is_live
    assert settings.llm_provider == "nvidia"
    assert settings.secret_value("nvidia_api_key") == "nvidia-key"


def test_default_openai_base_url_includes_the_api_version() -> None:
    settings = Settings(_env_file=None)

    assert settings.openai_base_url == "https://api.openai.com/v1"
    assert settings.nvidia_base_url == "https://integrate.api.nvidia.com/v1"


def test_in_app_worker_is_opt_in() -> None:
    assert Settings(_env_file=None).run_worker_in_app is False
    assert Settings(run_worker_in_app=True, _env_file=None).run_worker_in_app is True


def test_unknown_intelligence_provider_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_provider="other", _env_file=None)


def test_live_startup_failure_does_not_log_secret_values(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("RESILINET_APP_ENV", "live")
    monkeypatch.setenv("RESILINET_LLM_PROVIDER", "openai")
    monkeypatch.setenv("RESILINET_AGENTBOXD_API_KEY", "super-secret-value")
    for variable in (
        "RESILINET_AGENTBOXD_WEBHOOK_SECRET",
        "RESILINET_MOJOAUTH_API_KEY",
        "RESILINET_OPENAI_API_KEY",
        "RESILINET_NVIDIA_API_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)

    with caplog.at_level(logging.INFO), pytest.raises(ValidationError):
        create_app()

    assert "super-secret-value" not in caplog.text


def test_test_and_replay_configurations_do_not_require_secrets() -> None:
    for app_env in ("test", "replay"):
        settings = Settings(app_env=app_env, _env_file=None)
        assert settings.app_env == app_env
        assert settings.is_live is False

"""Validated environment configuration.

Secrets have no defaults. They are optional in ``test`` and ``replay`` so the
offline demo and the test suite never require provider credentials, and are
mandatory in ``live`` so a misconfigured deployment fails at startup instead
of silently running against Fakes.
"""

from __future__ import annotations

from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["test", "replay", "live"]

IntelligenceProviderName = Literal["openai", "nvidia"]

#: Settings that must carry a value before the service may run in live mode,
#: regardless of which intelligence provider is selected. MojoAuth is not here
#: yet: no identity adapter or action page consumes it until Phase 3, so
#: requiring it would block the email-to-reply path without adding safety.
LIVE_REQUIRED_SECRETS: tuple[str, ...] = (
    "agentboxd_api_key",
    "agentboxd_webhook_secret",
)

#: The provider-specific secret required in live mode.
PROVIDER_SECRETS: dict[IntelligenceProviderName, str] = {
    "openai": "openai_api_key",
    "nvidia": "nvidia_api_key",
}


class Settings(BaseSettings):
    """Process configuration, read from ``RESILINET_*`` variables.

    Environment variables use the ``RESILINET_`` prefix, for example
    ``RESILINET_APP_ENV=live`` or ``RESILINET_DATABASE_URL=...``.
    """

    model_config = SettingsConfigDict(
        env_prefix="RESILINET_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: AppEnv = "test"
    database_url: str = "sqlite+aiosqlite:///./resilinet.db"
    public_base_url: str = "http://localhost:8000"
    incident_key: str = Field(default="demo-incident", min_length=1, max_length=120)
    momen_origins: tuple[str, ...] = ()
    log_level: str = "INFO"
    llm_provider: IntelligenceProviderName = "openai"
    run_worker_in_app: bool = True

    agentboxd_base_url: str = "https://api.agentboxd.com"
    mojoauth_base_url: str = "https://api.mojoauth.com"
    openai_model: str = "gpt-4.1-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    nvidia_model: str = "meta/llama-3.3-70b-instruct"
    nvidia_fallback_models: tuple[str, ...] = (
        "nvidia/llama-3.3-nemotron-super-49b-v1.5",
        "nvidia/nemotron-3.5-lightning-30b-a3b",
        "openai/gpt-oss-120b",
    )
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nominatim_base_url: str = "https://nominatim.openstreetmap.org"
    nominatim_contact_email: str | None = None

    agentboxd_api_key: SecretStr | None = None
    agentboxd_webhook_secret: SecretStr | None = None
    mojoauth_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    nvidia_api_key: SecretStr | None = None

    request_timeout_seconds: float = Field(default=20.0, gt=0, le=120)

    @model_validator(mode="after")
    def _require_live_secrets(self) -> Self:
        # Live mode fails fast for the providers the running path uses. The
        # intelligence provider is selectable, so only its own key is
        # mandatory; MojoAuth remains optional until the identity adapter and
        # action pages land in Phase 3.
        if self.app_env != "live":
            return self
        required = (*LIVE_REQUIRED_SECRETS, PROVIDER_SECRETS[self.llm_provider])
        missing = [
            name
            for name in required
            if (value := getattr(self, name)) is None or not value.get_secret_value().strip()
        ]
        if missing:
            raise ValueError(
                "live mode requires these settings: " + ", ".join(missing)
            )
        scheme = urlsplit(self.database_url.strip()).scheme.lower()
        if not (scheme == "postgres" or scheme.startswith("postgresql")):
            raise ValueError("live mode requires a PostgreSQL database URL")
        return self

    @property
    def is_live(self) -> bool:
        return self.app_env == "live"

    @property
    def is_replay(self) -> bool:
        return self.app_env == "replay"

    def public_url(self, path: str) -> str:
        """Join a path onto the configured public base URL."""
        return f"{self.public_base_url.rstrip('/')}/{path.lstrip('/')}"

    def secret_value(self, name: str) -> str | None:
        """Return a secret's plain value for an adapter, never for logging."""
        value = getattr(self, name, None)
        if value is None:
            return None
        if isinstance(value, SecretStr):
            stripped = value.get_secret_value().strip()
            return stripped or None
        text = str(value).strip()
        return text or None

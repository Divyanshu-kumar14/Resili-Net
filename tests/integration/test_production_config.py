"""Production deployment safety checks (Phase 6, Task 25).

Locks the production guards the Render Blueprint relies on:

- wildcard live CORS is rejected at settings validation;
- session cookies carry ``Secure`` in live mode only;
- live mode refuses to start without every secret the running path uses;
- application startup never seeds demo data (provisioning is manual);
- live mode never constructs replay/fake providers;
- the Blueprint keeps secrets out of the repo and migrations in the
  free-plan-safe start command.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from resilinet.adapters.agentboxd import AgentboxdInboxProvider
from resilinet.adapters.fakes import FakeInboxProvider, FakeIntelligenceProvider
from resilinet.adapters.nvidia import NvidiaIntelligenceProvider
from resilinet.adapters.openai import OpenAIIntelligenceProvider
from resilinet.composition import build_inbox_provider, build_intelligence_provider
from resilinet.config import Settings
from resilinet.security import session_cookie_header, session_cookie_secure

ROOT = Path(__file__).resolve().parents[2]

_LIVE_SECRETS = {
    "agentboxd_api_key": "agentboxd-key",
    "agentboxd_webhook_secret": "webhook-secret",
    "mojoauth_api_key": "mojoauth-key",
    "nominatim_contact_email": "ops@example.org",
}


def _live_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "live",
        "database_url": "postgresql+asyncpg://user:pass@localhost/resilinet",
        "public_base_url": "https://resilinet-web.onrender.com",
        "llm_provider": "openai",
        "openai_api_key": "openai-key",
        **_LIVE_SECRETS,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def test_live_rejects_wildcard_momen_origin() -> None:
    with pytest.raises(ValidationError, match=r"\*"):
        _live_settings(momen_origins=("*",))


@pytest.mark.parametrize("origins", [("https://*.evil.com",), ("* .example.com",)])
def test_live_rejects_embedded_wildcard_momen_origins(origins: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError, match=r"\*"):
        _live_settings(momen_origins=origins)


def test_live_session_cookies_are_secure() -> None:
    live = _live_settings()
    assert session_cookie_secure(live) is True
    header = session_cookie_header("token", max_age_seconds=60, secure=True)
    assert "Secure" in header
    assert "HttpOnly" in header
    assert "SameSite=Lax" in header


def test_non_live_cookies_may_omit_secure_for_local_http() -> None:
    assert session_cookie_secure(Settings(app_env="test", _env_file=None)) is False


@pytest.mark.parametrize("provider", ["openai", "nvidia"])
def test_live_requires_the_selected_providers_key(provider: str) -> None:
    key_field = "openai_api_key" if provider == "openai" else "nvidia_api_key"
    with pytest.raises(ValidationError, match=key_field):
        _live_settings(llm_provider=provider, **{key_field: None})


def test_live_requires_agentboxd_mojoauth_and_postgres() -> None:
    with pytest.raises(ValidationError):
        Settings(app_env="live", _env_file=None)
    with pytest.raises(ValidationError, match="PostgreSQL"):
        _live_settings(database_url="sqlite+aiosqlite:///./local.db")


def test_app_startup_performs_no_demo_seed() -> None:
    offenders = [
        path
        for path in (ROOT / "src" / "resilinet").rglob("*.py")
        if re.search(
            r"^\s*(?:import|from)\s+[\w.]*\b(seed_demo|provision)\b",
            path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    ]
    assert offenders == []


def test_live_never_builds_replay_or_fake_providers() -> None:
    live_openai = _live_settings()
    assert isinstance(build_intelligence_provider(live_openai), OpenAIIntelligenceProvider)
    assert isinstance(build_inbox_provider(live_openai), AgentboxdInboxProvider)

    live_nvidia = _live_settings(llm_provider="nvidia", openai_api_key=None, nvidia_api_key="nv-key")
    assert isinstance(build_intelligence_provider(live_nvidia), NvidiaIntelligenceProvider)

    replay_with_keys = Settings(
        app_env="replay",
        agentboxd_api_key="key",
        agentboxd_webhook_secret="secret",
        openai_api_key="key",
        _env_file=None,
    )
    assert isinstance(build_inbox_provider(replay_with_keys), FakeInboxProvider)
    assert isinstance(build_intelligence_provider(replay_with_keys), FakeIntelligenceProvider)


def _load_blueprint() -> dict[str, object]:
    return yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_render_blueprint_keeps_secrets_out_of_the_repo() -> None:
    blueprint = _load_blueprint()
    assert set(blueprint.keys()) >= {"databases", "services"}
    services = blueprint["services"]  # type: ignore[index]
    assert isinstance(services, list) and len(services) == 1
    service = services[0]
    assert service["type"] == "web"
    assert service["runtime"] == "python"
    assert service["healthCheckPath"] == "/api/v1/health"

    database = blueprint["databases"][0]  # type: ignore[index]
    assert service.get("region") == database.get("region")

    env_vars = service["envVars"]
    from_db = [entry for entry in env_vars if "fromDatabase" in entry]
    assert len(from_db) == 1
    assert from_db[0]["fromDatabase"]["property"] == "connectionString"

    secret_keys = {
        "RESILINET_AGENTBOXD_API_KEY",
        "RESILINET_AGENTBOXD_WEBHOOK_SECRET",
        "RESILINET_MOJOAUTH_API_KEY",
        "RESILINET_OPENAI_API_KEY",
        "RESILINET_NVIDIA_API_KEY",
        "RESILINET_NOMINATIM_CONTACT_EMAIL",
    }
    declared = {entry["key"]: entry for entry in env_vars}
    assert secret_keys <= set(declared)
    for key in secret_keys:
        assert declared[key].get("sync") is False, key
        assert "value" not in declared[key], key

    non_secret_values = {
        "RESILINET_APP_ENV": "live",
        "RESILINET_LLM_PROVIDER": "nvidia",
    }
    for key, expected in non_secret_values.items():
        assert declared[key]["value"] == expected, key


def test_render_blueprint_runs_migrations_without_predeploy() -> None:
    """Free-plan services cannot use ``preDeployCommand`` (paid only), so the
    migration must stay in the start command. If the service moves to a paid
    plan, prefer ``preDeployCommand`` and keep the start command migration-free.
    """
    service = _load_blueprint()["services"][0]  # type: ignore[index]
    assert "preDeployCommand" not in service
    assert "alembic upgrade head" in service["startCommand"]
    assert "uvicorn resilinet.main:app" in service["startCommand"]


def test_live_factories_raise_instead_of_returning_fakes() -> None:
    live_openai = _live_settings()
    live_openai.openai_api_key = None  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="RESILINET_OPENAI_API_KEY"):
        build_intelligence_provider(live_openai)

    live_nvidia = _live_settings(llm_provider="nvidia", openai_api_key=None, nvidia_api_key="nv-key")
    live_nvidia.nvidia_api_key = None  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="RESILINET_NVIDIA_API_KEY"):
        build_intelligence_provider(live_nvidia)

    live_inbox = _live_settings()
    live_inbox.agentboxd_api_key = None  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="Agentboxd"):
        build_inbox_provider(live_inbox)


def _load_smoke_module():  # type: ignore[no-untyped-def]
    spec = spec_from_file_location("smoke_live", ROOT / "scripts" / "smoke_live.py")
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_preflight_requires_a_momen_origin() -> None:
    smoke_live = _load_smoke_module()
    without_origin = _live_settings()
    assert any(
        "MOMEN_ORIGINS" in blocker
        for blocker in smoke_live.preflight(without_origin, "tester@example.org")
    )
    with_origin = _live_settings(momen_origins=("https://momen.example",))
    assert smoke_live.preflight(with_origin, "tester@example.org") == []


def test_smoke_live_refuses_non_live_without_fakes() -> None:
    env = {**os.environ, "RESILINET_APP_ENV": "test"}
    result = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_live.py",
            "--mode",
            "live",
            "--recipient",
            "tester@example.org",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
        env=env,
    )
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report["preflight_blockers"]
    assert "tester@example.org" not in result.stdout
    assert "***@example.org" in result.stdout

"""Audit-only ASGI factory: the real app with test-double providers.

Used solely by scripts/verify_accessibility.mjs so authenticated pages
(operator reviews, action forms) can be audited locally. Production
refuses to serve action pages without a configured auth provider; this
factory injects the deterministic fakes, exactly as the test suite does.

    uv run uvicorn scripts.run_audit_server:create_audit_app --factory \
        --host 127.0.0.1 --port 8791
"""

from __future__ import annotations

from resilinet.adapters.fakes import FakeAuthProvider, FakeInboxProvider
from resilinet.config import Settings
from resilinet.db import create_engine, create_session_factory
from resilinet.main import Container, create_app


def create_audit_app():  # type: ignore[no-untyped-def]
    settings = Settings()
    engine = create_engine(settings)
    container = Container(
        settings=settings,
        session_factory=create_session_factory(engine),
        inbox=FakeInboxProvider(webhook_secret="audit"),
        engine=engine,
        auth=FakeAuthProvider(),
    )
    return create_app(settings, container)

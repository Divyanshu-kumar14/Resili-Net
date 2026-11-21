"""Run the durable message and outbox loops as a standalone process.

On Render's free plan a background worker service is unavailable; set
``RESILINET_RUN_WORKER_IN_APP=true`` instead so the web process starts the same
runtime. This entrypoint is for paid worker services, local testing, and
platforms that support long-running processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

from resilinet.composition import build_inbox_provider
from resilinet.config import Settings
from resilinet.db import create_engine, create_session_factory
from resilinet.observability import configure_logging
from resilinet.workers.runtime import run_worker


async def _run(settings: Settings) -> None:
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    inbox = build_inbox_provider(settings)
    try:
        await run_worker(settings, session_factory, inbox)
    finally:
        await inbox.aclose()
        await engine.dispose()


def main() -> int:
    settings = Settings()
    if not settings.is_live:
        print("RESILINET_APP_ENV must be live to run the worker", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import ProgrammingError

from resilinet.workers import runtime
from resilinet.workers.runtime import (
    WorkerRuntime,
    close_worker_runtime,
    resolve_incident_id,
    run_worker_loop,
)


class _Session:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def scalar(self, statement: object) -> Any:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _Factory:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def __call__(self) -> _Session:
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        return _Session(outcome)


class _Worker:
    def __init__(self, stop: asyncio.Event) -> None:
        self.stop = stop
        self.calls = 0

    async def run_once(self) -> int:
        self.calls += 1
        self.stop.set()
        return 1


class _Outbox:
    async def deliver_due(self) -> bool:
        return False


class _Geocoder:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _Intelligence:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


async def test_resolve_incident_id_returns_the_provisioned_incident() -> None:
    expected = uuid4()
    factory = _Factory([expected])
    settings = SimpleNamespace(incident_key="demo-incident")

    assert await resolve_incident_id(settings, factory) == expected
    assert factory.calls == 1


async def test_resolve_incident_id_retries_then_exits_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "INCIDENT_WAIT_SECONDS", 0.03)
    monkeypatch.setattr(runtime, "SCHEMA_WAIT_SECONDS", 0.03)
    monkeypatch.setattr(runtime, "SCHEMA_RETRY_SECONDS", 0.005)
    factory = _Factory([None])
    settings = SimpleNamespace(incident_key="demo-incident")

    with pytest.raises(SystemExit, match="not provisioned"):
        await resolve_incident_id(settings, factory)

    assert factory.calls > 1


async def test_resolve_incident_id_retries_then_exits_when_schema_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "INCIDENT_WAIT_SECONDS", 0.03)
    monkeypatch.setattr(runtime, "SCHEMA_WAIT_SECONDS", 0.03)
    monkeypatch.setattr(runtime, "SCHEMA_RETRY_SECONDS", 0.005)
    failure = ProgrammingError("SELECT 1", {}, Exception("undefined table"))
    factory = _Factory([failure])
    settings = SimpleNamespace(incident_key="demo-incident")

    with pytest.raises(SystemExit, match="schema is not ready"):
        await resolve_incident_id(settings, factory)

    assert factory.calls > 1


async def test_worker_loop_stops_after_the_event_is_set() -> None:
    stop = asyncio.Event()
    worker = _Worker(stop)
    runtime_state = WorkerRuntime(
        worker=worker,  # type: ignore[arg-type]
        outbox=_Outbox(),  # type: ignore[arg-type]
        geocoder=_Geocoder(),  # type: ignore[arg-type]
        intelligence=_Intelligence(),  # type: ignore[arg-type]
        stop=stop,
    )

    await asyncio.wait_for(run_worker_loop(runtime_state), timeout=1.0)

    assert worker.calls == 1


async def test_close_worker_runtime_closes_owned_clients() -> None:
    geocoder = _Geocoder()
    intelligence = _Intelligence()
    runtime_state = WorkerRuntime(
        worker=_Worker(asyncio.Event()),  # type: ignore[arg-type]
        outbox=_Outbox(),  # type: ignore[arg-type]
        geocoder=geocoder,  # type: ignore[arg-type]
        intelligence=intelligence,  # type: ignore[arg-type]
        stop=asyncio.Event(),
    )

    await close_worker_runtime(runtime_state)

    assert geocoder.closed
    assert intelligence.closed

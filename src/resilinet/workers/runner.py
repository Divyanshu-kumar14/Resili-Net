"""Durable job runner.

A job is leased in a short transaction, its handler runs outside any database
transaction, and the outcome is recorded in another short transaction. A worker
that dies mid-handler leaves a lease behind; the next ``run_once`` reclaims the
job once the lease expires. Terminal writes are fenced on
``(status=leased, lease_owner, attempts)`` so a stale worker cannot overwrite a
job another worker reclaimed and finished.

``SKIP LOCKED`` is a no-op on SQLite, which serializes writers anyway: run one
consumer for local development. PostgreSQL provides the real concurrent claim
guarantee, proven by the Task 22 suite.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db import utcnow
from ..models import Job, JobStatus, JobType
from ..observability import new_trace_id, reset_trace_id, set_trace_id
from ..ports import CritiqueError, ExtractionError, ProviderUnavailableError
from ..services.events import record_event

logger = logging.getLogger("resilinet.workers")

MAX_BACKOFF_SECONDS = 300


class PermanentJobError(RuntimeError):
    """The job cannot succeed on retry; dead-letter it immediately."""


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 1)


@dataclass(frozen=True, slots=True)
class JobClaim:
    id: UUID
    incident_id: UUID
    type: JobType
    payload: Mapping[str, Any]
    attempts: int
    max_attempts: int


JobHandler = Callable[[JobClaim], Awaitable[None]]


class DurableWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        handlers: Mapping[JobType, JobHandler],
        *,
        worker_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 60,
        retry_base_seconds: float = 2.0,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._handlers = dict(handlers)
        self._worker_id = worker_id or f"worker-{uuid4().hex[:12]}"
        self._clock = clock or utcnow
        self._lease_seconds = lease_seconds
        self._retry_base_seconds = retry_base_seconds
        self._jitter = jitter or random.random

    async def run_once(self) -> int:
        """Claim and run at most one due job. Returns 1 when work was done.

        Outcome classification is typed, never blind: permanent errors
        (``PermanentJobError``, schema-invalid extraction/critique, and
        provider failures marked non-retryable) dead-letter immediately;
        everything else retries with bounded exponential backoff until
        ``max_attempts`` and then dead-letters. While the handler runs, a
        heartbeat keeps extending the lease so a long job is not reclaimed
        underneath its owner.
        """
        claim = await self._claim()
        if claim is None:
            return 0
        token = set_trace_id(new_trace_id())
        started = time.monotonic()
        heartbeat = asyncio.create_task(self._heartbeat(claim))
        try:
            handler = self._handlers.get(claim.type)
            if handler is None:
                await self._dead_letter(
                    claim,
                    f"no handler registered for {claim.type.value}",
                    duration_ms=_elapsed_ms(started),
                )
                return 1
            try:
                await handler(claim)
            except PermanentJobError as exc:
                await self._dead_letter(
                    claim, str(exc), duration_ms=_elapsed_ms(started)
                )
            except (ExtractionError, CritiqueError) as exc:
                # Schema-invalid provider output cannot be fixed by retrying
                # the same input unchanged.
                await self._dead_letter(
                    claim,
                    f"{type(exc).__name__}: {exc}",
                    duration_ms=_elapsed_ms(started),
                )
            except ProviderUnavailableError as exc:
                if exc.retryable:
                    await self._schedule_retry_or_dead_letter(
                        claim, exc, duration_ms=_elapsed_ms(started)
                    )
                else:
                    await self._dead_letter(
                        claim,
                        f"{type(exc).__name__}: {exc}",
                        duration_ms=_elapsed_ms(started),
                    )
            except Exception as exc:
                await self._schedule_retry_or_dead_letter(
                    claim, exc, duration_ms=_elapsed_ms(started)
                )
            else:
                await self._succeed(claim, duration_ms=_elapsed_ms(started))
            return 1
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            reset_trace_id(token)

    async def _heartbeat(self, claim: JobClaim) -> None:
        """Extend the lease while the handler runs, fenced like _finish."""
        interval = max(0.05, self._lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            now = self._clock()
            async with self._session_factory() as session, session.begin():
                await session.execute(
                    sa.update(Job)
                    .where(
                        Job.id == claim.id,
                        Job.status == JobStatus.LEASED,
                        Job.lease_owner == self._worker_id,
                        Job.attempts == claim.attempts,
                    )
                    .values(
                        lease_expires_at=now
                        + timedelta(seconds=self._lease_seconds)
                    )
                )

    async def _claim(self) -> JobClaim | None:
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            job = await session.scalar(
                sa.select(Job)
                .where(
                    sa.or_(
                        sa.and_(
                            Job.status.in_(
                                [JobStatus.PENDING, JobStatus.RETRY_SCHEDULED]
                            ),
                            Job.available_at <= now,
                        ),
                        sa.and_(
                            Job.status == JobStatus.LEASED,
                            Job.lease_expires_at.is_not(None),
                            Job.lease_expires_at < now,
                        ),
                    )
                )
                .order_by(Job.available_at, Job.created_at, Job.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return None
            job.status = JobStatus.LEASED
            job.lease_owner = self._worker_id
            job.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            job.attempts += 1
            await session.flush()
            return JobClaim(
                id=job.id,
                incident_id=job.incident_id,
                type=job.type,
                payload=dict(job.payload),
                attempts=job.attempts,
                max_attempts=job.max_attempts,
            )

    async def _succeed(self, claim: JobClaim, *, duration_ms: float = 0.0) -> None:
        updated = await self._finish(
            claim,
            status=JobStatus.SUCCEEDED,
            available_at=None,
            last_error=None,
            event_type="job.succeeded",
            payload={
                "job_id": str(claim.id),
                "job_type": claim.type.value,
                "duration_ms": duration_ms,
            },
        )
        if not updated:
            logger.warning(
                "job_result_discarded",
                extra={"job_id": str(claim.id), "job_type": claim.type.value},
            )

    async def _finish(
        self,
        claim: JobClaim,
        *,
        status: JobStatus,
        available_at: datetime | None,
        last_error: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> bool:
        """Write a terminal state only if this worker still owns the lease.

        The conditional update fences a stale worker whose lease expired while
        another worker reclaimed and finished the job. ``rowcount``/RETURNING
        makes the check atomic on PostgreSQL and SQLite.
        """
        values: dict[str, Any] = {
            "status": status,
            "lease_owner": None,
            "lease_expires_at": None,
            "last_error": last_error,
        }
        if available_at is not None:
            values["available_at"] = available_at
        async with self._session_factory() as session, session.begin():
            updated = await session.scalar(
                sa.update(Job)
                .where(
                    Job.id == claim.id,
                    Job.status == JobStatus.LEASED,
                    Job.lease_owner == self._worker_id,
                    Job.attempts == claim.attempts,
                )
                .values(**values)
                .returning(Job.id)
            )
            if updated is None:
                return False
            await record_event(
                session,
                incident_id=claim.incident_id,
                event_type=event_type,
                aggregate_type="job",
                aggregate_id=claim.id,
                payload=payload,
            )
        return True

    async def _schedule_retry_or_dead_letter(
        self, claim: JobClaim, error: Exception, *, duration_ms: float = 0.0
    ) -> None:
        detail = f"{type(error).__name__}: {error}"[:400]
        if claim.attempts >= claim.max_attempts:
            await self._dead_letter(claim, detail, duration_ms=duration_ms)
            return
        now = self._clock()
        delay = min(
            MAX_BACKOFF_SECONDS,
            self._retry_base_seconds * (2 ** (claim.attempts - 1)),
        )
        delay += self._jitter() * self._retry_base_seconds
        updated = await self._finish(
            claim,
            status=JobStatus.RETRY_SCHEDULED,
            available_at=now + timedelta(seconds=delay),
            last_error=detail,
            event_type="job.retry_scheduled",
            payload={
                "job_id": str(claim.id),
                "job_type": claim.type.value,
                "attempts": claim.attempts,
                "error_type": type(error).__name__,
                "duration_ms": duration_ms,
            },
        )
        if updated:
            logger.warning(
                "job_retry_scheduled",
                extra={"job_id": str(claim.id), "job_type": claim.type.value},
            )

    async def _dead_letter(
        self, claim: JobClaim, detail: str, *, duration_ms: float = 0.0
    ) -> None:
        updated = await self._finish(
            claim,
            status=JobStatus.DEAD_LETTER,
            available_at=None,
            last_error=detail[:400],
            event_type="job.dead_letter",
            payload={
                "job_id": str(claim.id),
                "job_type": claim.type.value,
                "attempts": claim.attempts,
                "error_type": detail.split(":", 1)[0][:60],
                "duration_ms": duration_ms,
            },
        )
        if updated:
            logger.error(
                "job_dead_letter",
                extra={"job_id": str(claim.id), "job_type": claim.type.value},
            )


__all__ = [
    "DurableWorker",
    "JobClaim",
    "JobHandler",
    "PermanentJobError",
]

"""Minimal operator review escalation (the full queue UI is Task 16)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ReviewCase, ReviewCaseKind, ReviewStatus
from .events import record_event


class ReviewService:
    """Create (or reuse) open review cases inside the caller's transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def escalate(
        self,
        *,
        incident_id: UUID,
        kind: ReviewCaseKind,
        reason: str,
        source_message_id: UUID | None = None,
        report_id: UUID | None = None,
        need_id: UUID | None = None,
        allocation_id: UUID | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> ReviewCase:
        """Return the open case for this escalation, creating it if needed.

        Retried jobs must not stack duplicate cases for the same target, so
        an existing open case of the same kind for the same need or source
        message is reused.
        """
        filters = [
            ReviewCase.incident_id == incident_id,
            ReviewCase.kind == kind,
            ReviewCase.status == ReviewStatus.OPEN,
        ]
        if need_id is not None:
            filters.append(ReviewCase.need_id == need_id)
        if source_message_id is not None:
            filters.append(ReviewCase.source_message_id == source_message_id)
        if need_id is not None or source_message_id is not None:
            existing = await self._session.scalar(
                sa.select(ReviewCase).where(*filters)
            )
            if existing is not None:
                return existing

        case = ReviewCase(
            id=uuid4(),
            incident_id=incident_id,
            kind=kind,
            status=ReviewStatus.OPEN,
            source_message_id=source_message_id,
            report_id=report_id,
            need_id=need_id,
            allocation_id=allocation_id,
            reason=reason[:500],
            payload=dict(payload or {}),
        )
        self._session.add(case)
        await self._session.flush()
        await record_event(
            self._session,
            incident_id=incident_id,
            event_type="review.escalated",
            aggregate_type="review_case",
            aggregate_id=case.id,
            payload={
                "review_case_id": str(case.id),
                "kind": kind.value,
                "need_id": str(need_id) if need_id else None,
            },
        )
        return case

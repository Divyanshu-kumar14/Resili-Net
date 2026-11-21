"""SQLAlchemy persistence model for ResiliNet.

Conventions:

* every aggregate is keyed by a UUID and its FKs are indexed;
* every timestamp is stored in UTC and read back timezone-aware;
* quantities use ``Numeric(20, 3)`` and checks reject impossible values;
* enum columns store the public string value and carry a CHECK constraint;
* secrets (session and grant tokens) are stored only as hashes;
* relationships are queried explicitly instead of using lazy ORM
  relationships, so an async session never performs implicit IO.

``reference_counters`` is the durable backing store for ``RN-``/``AL-`` codes.
It is a table rather than a PostgreSQL sequence so SQLite development uses the
same code path and a rolled-back transaction does not burn a public number.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .db import UTCDateTime, utcnow
from .domain.quantities import QuantitySource
from .schemas import (
    AllocationStatus,
    EntityType,
    GeoConfidence,
    NeedStatus,
    ReportKind,
    Resource,
    Urgency,
)


class Base(DeclarativeBase):
    """Declarative base; ``alembic/env.py`` uses ``Base.metadata``."""


# -- persistence-only vocabularies ------------------------------------------------


class IncidentStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class MessageStatus(StrEnum):
    RECEIVED = "received"
    DEFERRED = "deferred"
    QUEUED = "queued"
    PROCESSED = "processed"
    CLARIFICATION_PENDING = "clarification_pending"
    NEEDS_REVIEW = "needs_review"
    THROTTLED = "throttled"
    IGNORED = "ignored"
    FAILED = "failed"


class JobType(StrEnum):
    PROCESS_MESSAGE = "process_message"
    FETCH_CONTENT = "fetch_content"


class JobStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTER = "dead_letter"


class OutboundStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ParticipantRoleKind(StrEnum):
    DEPOT_COORDINATOR = "depot_coordinator"
    ENTITY_COORDINATOR = "entity_coordinator"
    OPERATOR = "operator"


class PendingActionKind(StrEnum):
    CONFIRM_ALLOCATION = "CONFIRM_ALLOCATION"
    DELIVER_ALLOCATION = "DELIVER_ALLOCATION"
    CANCEL_ALLOCATION = "CANCEL_ALLOCATION"
    CANCEL_NEED = "CANCEL_NEED"
    UPDATE_SUPPLY = "UPDATE_SUPPLY"


class PendingActionStatus(StrEnum):
    PENDING = "PENDING"
    CONSUMED = "CONSUMED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ReviewCaseKind(StrEnum):
    CLARIFICATION = "clarification"
    MEDICAL = "medical"
    ROLE_ASSIGNMENT = "role_assignment"
    QUANTITY = "quantity"
    DUPLICATE = "duplicate"
    CRITIC = "critic"
    DEAD_LETTER = "dead_letter"
    OTHER = "other"


class ReviewStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class VerificationStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    EXPIRED = "expired"
    FAILED = "failed"


def enum_column(
    enum_cls: type[StrEnum],
    name: str,
    *,
    length: int = 32,
) -> sa.Enum:
    """A portable enum column: VARCHAR plus CHECK, storing the public value."""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
    )


class UUIDPrimaryKey:
    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid4)


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


# -- incident, participants, and intake ------------------------------------------


class Incident(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "incidents"

    key: Mapped[str] = mapped_column(sa.String(120), unique=True)
    name: Mapped[str] = mapped_column(sa.String(200))
    status: Mapped[IncidentStatus] = mapped_column(
        enum_column(IncidentStatus, "incident_status"), default=IncidentStatus.ACTIVE
    )
    simulation: Mapped[bool] = mapped_column(default=False)


class Sender(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "senders"
    __table_args__ = (sa.UniqueConstraint("incident_id", "address", name="uq_senders_address"),)

    incident_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    address: Mapped[str] = mapped_column(sa.String(320))
    verified: Mapped[bool] = mapped_column(default=False)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ParticipantRole(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "participant_roles"
    __table_args__ = (
        sa.UniqueConstraint(
            "incident_id", "sender_id", "role", "entity_id", name="uq_participant_roles_scoped"
        ),
        sa.Index(
            "uq_participant_roles_incident_wide",
            "incident_id",
            "sender_id",
            "role",
            unique=True,
            sqlite_where=sa.text("entity_id IS NULL"),
            postgresql_where=sa.text("entity_id IS NULL"),
        ),
    )

    incident_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    sender_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("senders.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[ParticipantRoleKind] = mapped_column(
        enum_column(ParticipantRoleKind, "participant_role_kind")
    )
    entity_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=True, index=True
    )


class Message(UUIDPrimaryKey, Timestamped, Base):
    """One inbound message, exactly once per provider message ID.

    ``raw_text`` keeps the provider content for audit and re-processing;
    ``body_text`` is the normalized text extraction receives. Neither is ever
    serialized into public output or operational logs.
    """

    __tablename__ = "messages"
    __table_args__ = (
        sa.UniqueConstraint(
            "incident_id", "provider_message_id", name="uq_messages_provider_message"
        ),
        sa.Index("ix_messages_thread", "incident_id", "thread_id"),
        sa.Index("ix_messages_received", "incident_id", "received_at"),
        sa.CheckConstraint(
            "clarification_attempts >= 0", name="ck_messages_clarification_attempts"
        ),
    )

    incident_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    sender_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("senders.id", ondelete="RESTRICT"), index=True
    )
    provider_message_id: Mapped[str] = mapped_column(sa.String(200))
    mailbox_id: Mapped[str] = mapped_column(sa.String(200))
    thread_id: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    subject: Mapped[str] = mapped_column(sa.String(500))
    body_text: Mapped[str] = mapped_column(sa.Text, default="")
    raw_text: Mapped[str] = mapped_column(sa.Text, default="")
    status: Mapped[MessageStatus] = mapped_column(
        enum_column(MessageStatus, "message_status"), default=MessageStatus.RECEIVED
    )
    withheld: Mapped[bool] = mapped_column(default=False)
    attachment_count: Mapped[int] = mapped_column(default=0)
    signature_verification: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime)
    reply_to_message_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    clarification_fields: Mapped[list[str]] = mapped_column(sa.JSON, default=list)
    clarification_attempts: Mapped[int] = mapped_column(default=0)
    parsed_json: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Report(UUIDPrimaryKey, Timestamped, Base):
    """The validated extraction of one resource line, before any domain change."""

    __tablename__ = "reports"
    __table_args__ = (
        sa.CheckConstraint("quantity IS NULL OR quantity >= 0", name="ck_reports_quantity"),
        sa.CheckConstraint(
            "headcount IS NULL OR headcount >= 0", name="ck_reports_headcount"
        ),
    )

    incident_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    sender_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("senders.id", ondelete="RESTRICT"), index=True
    )
    entity_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("entities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[ReportKind] = mapped_column(enum_column(ReportKind, "report_kind"))
    resource: Mapped[Resource] = mapped_column(enum_column(Resource, "report_resource"))
    quantity: Mapped[Decimal | None] = mapped_column(sa.Numeric(20, 3), nullable=True)
    unit: Mapped[str | None] = mapped_column(sa.String(40), nullable=True)
    quantity_source: Mapped[QuantitySource | None] = mapped_column(
        enum_column(QuantitySource, "quantity_source"), nullable=True
    )
    qualifier: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    raw_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    entity_name: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    entity_type: Mapped[EntityType] = mapped_column(
        enum_column(EntityType, "report_entity_type"), default=EntityType.OTHER
    )
    location_text: Mapped[str | None] = mapped_column(sa.String(300), nullable=True)
    lat: Mapped[Decimal | None] = mapped_column(sa.Numeric(9, 6), nullable=True)
    lon: Mapped[Decimal | None] = mapped_column(sa.Numeric(9, 6), nullable=True)
    geo_confidence: Mapped[GeoConfidence] = mapped_column(
        enum_column(GeoConfidence, "report_geo_confidence"), default=GeoConfidence.NONE
    )
    location_source: Mapped[str | None] = mapped_column(sa.String(40), nullable=True)
    urgency: Mapped[Urgency] = mapped_column(
        enum_column(Urgency, "report_urgency"), default=Urgency.UNKNOWN
    )
    headcount: Mapped[int | None] = mapped_column(nullable=True)
    confidence: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)


class Entity(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "entities"
    __table_args__ = (
        sa.UniqueConstraint("incident_id", "normalized_name", name="uq_entities_name"),
    )

    incident_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(sa.String(200))
    normalized_name: Mapped[str] = mapped_column(sa.String(200))
    entity_type: Mapped[EntityType] = mapped_column(
        enum_column(EntityType, "entity_type_kind"), default=EntityType.OTHER
    )
    location_text: Mapped[str | None] = mapped_column(sa.String(300), nullable=True)
    lat: Mapped[Decimal | None] = mapped_column(sa.Numeric(9, 6), nullable=True)
    lon: Mapped[Decimal | None] = mapped_column(sa.Numeric(9, 6), nullable=True)
    geo_confidence: Mapped[GeoConfidence] = mapped_column(
        enum_column(GeoConfidence, "entity_geo_confidence"), default=GeoConfidence.NONE
    )
    location_source: Mapped[str | None] = mapped_column(sa.String(40), nullable=True)


class Need(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "needs"
    __table_args__ = (
        sa.UniqueConstraint("ref", name="uq_needs_ref"),
        sa.CheckConstraint("quantity >= 0", name="ck_needs_quantity"),
        sa.Index("ix_needs_incident_status", "incident_id", "status"),
    )

    incident_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    entity_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("entities.id", ondelete="RESTRICT"), index=True
    )
    source_report_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("reports.id", ondelete="SET NULL"), nullable=True
    )
    ref: Mapped[str] = mapped_column(sa.String(16))
    resource: Mapped[Resource] = mapped_column(enum_column(Resource, "need_resource"))
    quantity: Mapped[Decimal] = mapped_column(sa.Numeric(20, 3))
    unit: Mapped[str] = mapped_column(sa.String(16))
    quantity_source: Mapped[QuantitySource] = mapped_column(
        enum_column(QuantitySource, "need_quantity_source")
    )
    headcount: Mapped[int | None] = mapped_column(nullable=True)
    urgency: Mapped[Urgency] = mapped_column(
        enum_column(Urgency, "need_urgency"), default=Urgency.UNKNOWN
    )
    status: Mapped[NeedStatus] = mapped_column(
        enum_column(NeedStatus, "need_status"), default=NeedStatus.OPEN
    )
    verified: Mapped[bool] = mapped_column(default=False)
    needs_human_review: Mapped[bool] = mapped_column(default=False)


class Supply(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "supplies"
    __table_args__ = (
        sa.UniqueConstraint(
            "incident_id", "entity_id", "resource", name="uq_supplies_entity_resource"
        ),
        sa.CheckConstraint("on_hand >= 0", name="ck_supplies_on_hand"),
    )

    incident_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    entity_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("entities.id", ondelete="RESTRICT"), index=True
    )
    source_report_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("reports.id", ondelete="SET NULL"), nullable=True
    )
    resource: Mapped[Resource] = mapped_column(enum_column(Resource, "supply_resource"))
    on_hand: Mapped[Decimal] = mapped_column(sa.Numeric(20, 3))
    unit: Mapped[str] = mapped_column(sa.String(16))


class Allocation(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "allocations"
    __table_args__ = (
        sa.UniqueConstraint("ref", name="uq_allocations_ref"),
        sa.CheckConstraint("quantity > 0", name="ck_allocations_quantity"),
        sa.CheckConstraint("delivered_quantity >= 0", name="ck_allocations_delivered"),
        sa.CheckConstraint(
            "delivered_quantity <= quantity", name="ck_allocations_delivered_le_quantity"
        ),
        sa.CheckConstraint("distance_km >= 0", name="ck_allocations_distance"),
    )

    incident_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    need_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("needs.id", ondelete="RESTRICT"), index=True
    )
    supply_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("supplies.id", ondelete="RESTRICT"), index=True
    )
    ref: Mapped[str] = mapped_column(sa.String(16))
    resource: Mapped[Resource] = mapped_column(enum_column(Resource, "allocation_resource"))
    quantity: Mapped[Decimal] = mapped_column(sa.Numeric(20, 3))
    unit: Mapped[str] = mapped_column(sa.String(16))
    delivered_quantity: Mapped[Decimal] = mapped_column(
        sa.Numeric(20, 3), default=Decimal("0")
    )
    distance_km: Mapped[Decimal] = mapped_column(sa.Numeric(10, 3))
    status: Mapped[AllocationStatus] = mapped_column(
        enum_column(AllocationStatus, "allocation_status"), default=AllocationStatus.PROPOSED
    )
    critic_verdict: Mapped[str] = mapped_column(sa.String(40), default="pending")


# -- email actions, reviews, and identity ---------------------------------------


class PendingAction(UUIDPrimaryKey, Timestamped, Base):
    """An immutable validated email action, stored before any domain mutation."""

    __tablename__ = "pending_actions"
    __table_args__ = (
        sa.Index("ix_pending_actions_status", "status", "created_at"),
    )

    kind: Mapped[PendingActionKind] = mapped_column(
        enum_column(PendingActionKind, "pending_action_kind", length=40)
    )
    incident_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    sender_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("senders.id", ondelete="RESTRICT"), index=True
    )
    source_message_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    source_report_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("reports.id", ondelete="SET NULL"), nullable=True
    )
    entity_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("entities.id", ondelete="SET NULL"), nullable=True
    )
    need_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("needs.id", ondelete="SET NULL"), nullable=True
    )
    allocation_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("allocations.id", ondelete="SET NULL"), nullable=True
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    payload_hash: Mapped[str] = mapped_column(sa.String(64))
    status: Mapped[PendingActionStatus] = mapped_column(
        enum_column(PendingActionStatus, "pending_action_status"),
        default=PendingActionStatus.PENDING,
    )
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ActionGrant(UUIDPrimaryKey, Timestamped, Base):
    """A bearer capability; only the SHA-256 hash of the token is stored."""

    __tablename__ = "action_grants"
    __table_args__ = (sa.UniqueConstraint("token_hash", name="uq_action_grants_token"),)

    kind: Mapped[PendingActionKind] = mapped_column(
        enum_column(PendingActionKind, "action_grant_kind", length=40)
    )
    incident_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    sender_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("senders.id", ondelete="RESTRICT"), index=True
    )
    entity_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("entities.id", ondelete="SET NULL"), nullable=True
    )
    need_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("needs.id", ondelete="SET NULL"), nullable=True
    )
    allocation_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("allocations.id", ondelete="SET NULL"), nullable=True
    )
    pending_action_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("pending_actions.id", ondelete="SET NULL"), nullable=True
    )
    payload_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    token_hash: Mapped[str] = mapped_column(sa.String(64))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class AuthSession(UUIDPrimaryKey, Timestamped, Base):
    """A server-side session; only the SHA-256 hash of the cookie is stored."""

    __tablename__ = "sessions"
    __table_args__ = (sa.UniqueConstraint("token_hash", name="uq_sessions_token"),)

    incident_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    sender_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("senders.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(sa.String(64))
    csrf_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class VerificationRequest(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "verification_requests"
    __table_args__ = (sa.Index("ix_verification_sender_status", "sender_id", "status"),)

    incident_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    sender_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("senders.id", ondelete="CASCADE"), index=True
    )
    provider_state_id: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    redirect_url: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    status: Mapped[VerificationStatus] = mapped_column(
        enum_column(VerificationStatus, "verification_status"),
        default=VerificationStatus.PENDING,
    )
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ReviewCase(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "review_cases"
    __table_args__ = (sa.Index("ix_review_cases_incident_status", "incident_id", "status"),)

    incident_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[ReviewCaseKind] = mapped_column(
        enum_column(ReviewCaseKind, "review_case_kind")
    )
    status: Mapped[ReviewStatus] = mapped_column(
        enum_column(ReviewStatus, "review_status"), default=ReviewStatus.OPEN
    )
    source_message_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    report_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("reports.id", ondelete="SET NULL"), nullable=True
    )
    need_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("needs.id", ondelete="SET NULL"), nullable=True
    )
    allocation_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("allocations.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str] = mapped_column(sa.String(500))
    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    resolution: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)
    resolved_by_sender_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("senders.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class EventLog(UUIDPrimaryKey, Base):
    """Append-only internal audit trail. Payloads never contain PII or tokens."""

    __tablename__ = "event_log"
    __table_args__ = (
        sa.Index("ix_event_log_incident_created", "incident_id", "created_at"),
        sa.Index("ix_event_log_type", "event_type"),
    )

    incident_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(sa.String(80))
    trace_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    actor_type: Mapped[str] = mapped_column(sa.String(40), default="system")
    actor_id: Mapped[UUID | None] = mapped_column(sa.Uuid, nullable=True)
    aggregate_type: Mapped[str | None] = mapped_column(sa.String(40), nullable=True)
    aggregate_id: Mapped[UUID | None] = mapped_column(sa.Uuid, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# -- durable work and outbound email --------------------------------------------


class Job(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        sa.UniqueConstraint("dedupe_key", name="uq_jobs_dedupe_key"),
        sa.Index("ix_jobs_due", "status", "available_at"),
        sa.Index("ix_jobs_lease", "status", "lease_expires_at"),
        sa.CheckConstraint("attempts >= 0", name="ck_jobs_attempts"),
        sa.CheckConstraint("max_attempts > 0", name="ck_jobs_max_attempts"),
    )

    incident_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[JobType] = mapped_column(enum_column(JobType, "job_type"))
    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    status: Mapped[JobStatus] = mapped_column(
        enum_column(JobStatus, "job_status"), default=JobStatus.PENDING
    )
    dedupe_key: Mapped[str | None] = mapped_column(sa.String(160), nullable=True)
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    lease_owner: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)


class OutboundEmail(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "outbound_emails"
    __table_args__ = (
        sa.UniqueConstraint("idempotency_key", name="uq_outbound_emails_idempotency"),
        sa.Index("ix_outbound_due", "status", "available_at"),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_outbound_emails_attempt_count"
        ),
    )

    incident_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sender_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("senders.id", ondelete="RESTRICT"), index=True
    )
    to_address: Mapped[str] = mapped_column(sa.String(320))
    subject: Mapped[str] = mapped_column(sa.String(500))
    body_text: Mapped[str] = mapped_column(sa.Text)
    kind: Mapped[str] = mapped_column(sa.String(120))
    #: The incident mailbox a direct (non-reply) email is sent from; replies
    #: take the mailbox from their inbound message instead.
    mailbox_id: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(sa.String(160))
    status: Mapped[OutboundStatus] = mapped_column(
        enum_column(OutboundStatus, "outbound_status"), default=OutboundStatus.PENDING
    )
    attempt_count: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    last_error: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)


class GeocodeCache(UUIDPrimaryKey, Base):
    __tablename__ = "geocode_cache"
    __table_args__ = (
        sa.UniqueConstraint(
            "incident_id", "normalized_location", name="uq_geocode_incident_location"
        ),
        sa.CheckConstraint(
            "(lat IS NULL AND lon IS NULL) OR (lat IS NOT NULL AND lon IS NOT NULL)",
            name="ck_geocode_pair",
        ),
    )

    incident_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    normalized_location: Mapped[str] = mapped_column(sa.String(300))
    lat: Mapped[Decimal | None] = mapped_column(sa.Numeric(9, 6), nullable=True)
    lon: Mapped[Decimal | None] = mapped_column(sa.Numeric(9, 6), nullable=True)
    confidence: Mapped[GeoConfidence] = mapped_column(
        enum_column(GeoConfidence, "geocode_confidence"), default=GeoConfidence.NONE
    )
    source: Mapped[str] = mapped_column(sa.String(40))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ReferenceCounter(Base):
    """Durable allocator for human-facing ``RN-``/``AL-`` codes."""

    __tablename__ = "reference_counters"

    kind: Mapped[str] = mapped_column(sa.String(2), primary_key=True)
    next_value: Mapped[int] = mapped_column(default=1)


__all__ = [
    "ActionGrant",
    "Allocation",
    "AuthSession",
    "Base",
    "Entity",
    "EventLog",
    "GeocodeCache",
    "Incident",
    "IncidentStatus",
    "Job",
    "JobStatus",
    "JobType",
    "Message",
    "MessageStatus",
    "Need",
    "OutboundEmail",
    "OutboundStatus",
    "ParticipantRole",
    "ParticipantRoleKind",
    "PendingAction",
    "PendingActionKind",
    "PendingActionStatus",
    "ReferenceCounter",
    "Report",
    "ReviewCase",
    "ReviewCaseKind",
    "ReviewStatus",
    "Sender",
    "Supply",
    "VerificationRequest",
    "VerificationStatus",
    "enum_column",
]

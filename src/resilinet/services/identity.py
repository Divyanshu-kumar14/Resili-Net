"""Identity verification, server-side sessions, and incident-scoped roles.

The provider proves control of an email address. It never grants depot or
operator authority: those are separate ``participant_roles`` rows. Sessions
store only a SHA-256 digest of the cookie, and the CSRF token is derived from
that digest so it cannot be obtained from a cross-site cookie alone.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, quote, urlsplit
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..db import transaction
from ..models import (
    AuthSession,
    Incident,
    Need,
    ParticipantRole,
    ParticipantRoleKind,
    Report,
    Sender,
    VerificationStatus,
)
from ..models import (
    VerificationRequest as VerificationRequestRow,
)
from ..ports import AdapterError, AuthProvider
from ..security import (
    Principal,
    derive_csrf_token,
    hash_token,
    local_redirect_path,
    new_opaque_token,
    normalize_email,
)
from .events import record_event

logger = logging.getLogger("resilinet.identity")


class IdentityError(RuntimeError):
    """A verification request or session cannot be served."""


@dataclass(frozen=True, slots=True)
class SessionGrant:
    session_id: UUID
    cookie_token: str
    csrf_token: str
    redirect_path: str
    expires_at: datetime


class IdentityService:
    """Persist verification state and issue hashed server-side sessions."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        auth: AuthProvider | None,
        settings: Settings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._auth = auth
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._verification_ttl = timedelta(
            minutes=settings.verification_ttl_minutes
        )
        self._session_ttl = timedelta(minutes=settings.session_ttl_minutes)

    @property
    def configured(self) -> bool:
        return self._auth is not None

    async def ensure_sender_verification(
        self, sender_id: UUID
    ) -> VerificationRequestRow | None:
        """Request at most one outstanding verification for a sender."""
        if self._auth is None:
            return None
        reservation = await self._reserve(sender_id, next_path="/", force=False)
        if reservation is None:
            return None
        request_id, needs_send = reservation
        if not needs_send:
            return await self._load_request(request_id)
        return await self._send_magic_link(request_id)

    async def start_operator_login(self, email: str) -> None:
        """Start a throttled verification for an operator intent.

        Always returns ``None``: callers render the same generic response
        whether or not the address is known, avoiding account enumeration.
        """
        if self._auth is None:
            return
        address = normalize_email(email)
        if not address or "@" not in address or len(address) > 320:
            return
        sender_id = await self._existing_sender_for_login(address)
        if sender_id is None:
            # Never create a sender or send provider mail for an arbitrary
            # address: only an existing participant may request a session.
            return
        if await self._global_login_throttled():
            return
        if await self._login_throttled(sender_id):
            return
        reservation = await self._reserve(
            sender_id, next_path="/operator/reviews", force=True
        )
        if reservation is None:
            return
        request_id, needs_send = reservation
        if needs_send:
            await self._send_magic_link(request_id)

    async def complete_callback(
        self, params: Mapping[str, str]
    ) -> SessionGrant | None:
        """Verify a provider callback and create one server-side session."""
        if self._auth is None:
            return None
        state_id = str(params.get("state_id", "")).strip()
        if not state_id:
            return None
        now = self._clock()
        async with self._session_factory() as session:
            request = await session.scalar(
                sa.select(VerificationRequestRow).where(
                    VerificationRequestRow.provider_state_id == state_id,
                    VerificationRequestRow.status == VerificationStatus.PENDING,
                )
            )
            if request is None:
                return None
            if request.created_at + self._verification_ttl <= now:
                request.status = VerificationStatus.EXPIRED
                request.updated_at = now
                await session.commit()
                return None
            sender = await session.get(Sender, request.sender_id)
            if sender is None:
                return None
            bound_address = sender.address
            redirect_path = _next_path(request.redirect_url)
            request_id = request.id

        try:
            identity = await self._auth.verify_callback({"state_id": state_id})
        except AdapterError:
            # A transient provider failure must not burn the link; the user can
            # retry the same magic link.
            logger.warning(
                "identity_callback_provider_failed",
                extra={"verification_request_id": str(request_id)},
            )
            return None

        if not identity.verified or normalize_email(identity.email) != normalize_email(
            bound_address
        ):
            await self._mark_failed(request_id)
            logger.warning(
                "identity_callback_binding_mismatch",
                extra={"verification_request_id": str(request_id)},
            )
            return None

        return await self._complete_session(
            request_id=request_id, redirect_path=redirect_path, now=now
        )

    async def pending_state_is_valid(self, state_id: str) -> bool:
        """Cheap read-only check used by the callback confirmation page.

        The GET page must not consume the link: mailbox scanners and link
        previewers follow URLs. Only the POST completes verification.
        """
        state = state_id.strip()
        if self._auth is None or not state:
            return False
        async with self._session_factory() as session:
            request = await session.scalar(
                sa.select(VerificationRequestRow).where(
                    VerificationRequestRow.provider_state_id == state,
                    VerificationRequestRow.status == VerificationStatus.PENDING,
                )
            )
            if request is None:
                return False
            return request.created_at + self._verification_ttl > self._clock()

    async def resolve_session(self, cookie_token: str | None) -> Principal | None:
        """Return the live principal behind a cookie, or ``None``."""
        if not cookie_token:
            return None
        now = self._clock()
        async with self._session_factory() as session:
            row = await session.scalar(
                sa.select(AuthSession).where(
                    AuthSession.token_hash == hash_token(cookie_token),
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > now,
                )
            )
            if row is None:
                return None
            sender = await session.get(Sender, row.sender_id)
            if sender is None:
                return None
            csrf_token = derive_csrf_token(row.token_hash)
            if row.csrf_hash is not None and row.csrf_hash != hash_token(csrf_token):
                return None
            return Principal(
                session_id=row.id,
                incident_id=row.incident_id,
                sender_id=sender.id,
                address=sender.address,
                expires_at=row.expires_at,
                csrf_token=csrf_token,
            )

    async def verify_csrf(self, principal: Principal, token: str | None) -> bool:
        from ..security import csrf_matches

        return csrf_matches(principal.csrf_token, token)

    async def logout(self, cookie_token: str | None, csrf_token: str | None) -> bool:
        """Revoke the session only when its own CSRF token is presented."""
        principal = await self.resolve_session(cookie_token)
        if principal is None or not await self.verify_csrf(principal, csrf_token):
            return False
        now = self._clock()
        async with transaction(self._session_factory) as session:
            row = await session.get(AuthSession, principal.session_id, with_for_update=True)
            if row is None or row.revoked_at is not None:
                return False
            row.revoked_at = now
            row.updated_at = now
            await record_event(
                session,
                incident_id=row.incident_id,
                event_type="identity.session_revoked",
                actor_type="sender",
                actor_id=row.sender_id,
                aggregate_type="session",
                aggregate_id=row.id,
                payload={"session_id": str(row.id)},
            )
        return True

    async def has_role(
        self,
        principal: Principal,
        role: ParticipantRoleKind,
        *,
        entity_id: UUID | None = None,
    ) -> bool:
        """Check an incident-scoped role; verification alone never qualifies."""
        if role is not ParticipantRoleKind.OPERATOR and entity_id is None:
            return False
        async with self._session_factory() as session:
            query = sa.select(ParticipantRole.id).where(
                ParticipantRole.incident_id == principal.incident_id,
                ParticipantRole.sender_id == principal.sender_id,
                ParticipantRole.role == role,
            )
            if role is ParticipantRoleKind.OPERATOR:
                query = query.where(ParticipantRole.entity_id.is_(None))
            else:
                query = query.where(ParticipantRole.entity_id == entity_id)
            return await session.scalar(query.limit(1)) is not None

    async def _existing_sender_for_login(self, address: str) -> UUID | None:
        async with self._session_factory() as session:
            incident = await session.scalar(
                sa.select(Incident.id).where(
                    Incident.key == self._settings.incident_key
                )
            )
            if incident is None:
                return None
            return await session.scalar(
                sa.select(Sender.id).where(
                    Sender.incident_id == incident, Sender.address == address
                )
            )

    async def _login_throttled(self, sender_id: UUID) -> bool:
        now = self._clock()
        window = now - timedelta(minutes=self._settings.operator_login_window_minutes)
        async with self._session_factory() as session:
            count = await session.scalar(
                sa.select(sa.func.count(VerificationRequestRow.id)).where(
                    VerificationRequestRow.sender_id == sender_id,
                    VerificationRequestRow.created_at >= window,
                )
            )
        return (count or 0) >= self._settings.operator_login_max_requests

    async def _global_login_throttled(self) -> bool:
        """Bound magic-link volume across rotated addresses and incidents."""
        now = self._clock()
        window = now - timedelta(minutes=self._settings.operator_login_window_minutes)
        async with self._session_factory() as session:
            count = await session.scalar(
                sa.select(sa.func.count(VerificationRequestRow.id)).where(
                    VerificationRequestRow.created_at >= window,
                    VerificationRequestRow.redirect_url.like(
                        "%next=/operator/reviews%"
                    ),
                )
            )
        return (count or 0) >= self._settings.operator_login_global_max_requests

    async def _reserve(
        self, sender_id: UUID, *, next_path: str, force: bool
    ) -> tuple[UUID, bool] | None:
        """Return ``(request_id, needs_provider_call)``."""
        now = self._clock()
        async with transaction(self._session_factory) as session:
            sender = await session.get(Sender, sender_id, with_for_update=True)
            if sender is None:
                raise IdentityError("unknown sender")
            if sender.verified and not force:
                return None
            pending = await session.scalar(
                sa.select(VerificationRequestRow)
                .where(
                    VerificationRequestRow.sender_id == sender.id,
                    VerificationRequestRow.status == VerificationStatus.PENDING,
                )
                .order_by(VerificationRequestRow.created_at.desc())
                .limit(1)
            )
            if pending is not None:
                if pending.created_at + self._verification_ttl <= now:
                    pending.status = VerificationStatus.EXPIRED
                    pending.updated_at = now
                    await session.flush()
                else:
                    # The latest intent wins for the callback destination; the
                    # provider state remains the same, so no duplicate email.
                    if _next_path(pending.redirect_url) != next_path:
                        pending.redirect_url = self._provider_redirect(next_path)
                        pending.updated_at = now
                        await session.flush()
                    return pending.id, pending.provider_state_id is None

            request = VerificationRequestRow(
                id=uuid4(),
                incident_id=sender.incident_id,
                sender_id=sender.id,
                provider_state_id=None,
                redirect_url=self._provider_redirect(next_path),
                status=VerificationStatus.PENDING,
                completed_at=None,
                created_at=now,
                updated_at=now,
            )
            try:
                async with session.begin_nested():
                    session.add(request)
                    await session.flush()
            except IntegrityError:
                # Another request won the one-pending-per-sender index.
                raced = await session.scalar(
                    sa.select(VerificationRequestRow.id)
                    .where(
                        VerificationRequestRow.sender_id == sender.id,
                        VerificationRequestRow.status == VerificationStatus.PENDING,
                    )
                    .limit(1)
                )
                if raced is None:
                    raise
                return raced, False
            await record_event(
                session,
                incident_id=sender.incident_id,
                event_type="identity.verification_requested",
                actor_type="sender",
                actor_id=sender.id,
                aggregate_type="verification_request",
                aggregate_id=request.id,
                payload={"verification_request_id": str(request.id)},
            )
            return request.id, True

    async def _send_magic_link(
        self, request_id: UUID
    ) -> VerificationRequestRow | None:
        if self._auth is None:
            return None
        now = self._clock()
        stale_claim = now - timedelta(minutes=2)
        email: str | None = None
        redirect_url: str | None = None
        async with transaction(self._session_factory) as session:
            claimed = await session.scalar(
                sa.update(VerificationRequestRow)
                .where(
                    VerificationRequestRow.id == request_id,
                    VerificationRequestRow.status == VerificationStatus.PENDING,
                    VerificationRequestRow.provider_state_id.is_(None),
                    sa.or_(
                        VerificationRequestRow.send_claimed_at.is_(None),
                        VerificationRequestRow.send_claimed_at < stale_claim,
                    ),
                )
                .values(send_claimed_at=now, updated_at=now)
                .returning(VerificationRequestRow.id)
            )
            if claimed is None:
                # Another call is already sending, or the request completed.
                email = None
            else:
                request = await session.get(
                    VerificationRequestRow, request_id, with_for_update=True
                )
                if request is None:  # pragma: no cover - the update matched a row
                    return None
                sender = await session.get(Sender, request.sender_id)
                if sender is None:
                    return None
                email = sender.address
                redirect_url = request.redirect_url or self._provider_redirect("/")
        if email is None:
            return await self._load_request(request_id)

        try:
            provider_request = await self._auth.send_magic_link(
                email, redirect_url or self._provider_redirect("/")
            )
        except AdapterError:
            await self._mark_failed(request_id)
            logger.warning(
                "identity_magic_link_failed",
                extra={"verification_request_id": str(request_id)},
            )
            return await self._load_request(request_id)

        now = self._clock()
        async with transaction(self._session_factory) as session:
            request = await session.get(
                VerificationRequestRow, request_id, with_for_update=True
            )
            if request is None:
                return None
            if request.status is VerificationStatus.PENDING:
                request.provider_state_id = provider_request.state_id
                request.send_claimed_at = None
                request.updated_at = now
                await session.flush()
            else:
                logger.warning(
                    "identity_magic_link_unbound",
                    extra={"verification_request_id": str(request_id)},
                )
            return request

    async def _complete_session(
        self, *, request_id: UUID, redirect_path: str, now: datetime
    ) -> SessionGrant | None:
        async with transaction(self._session_factory) as session:
            request = await session.get(
                VerificationRequestRow, request_id, with_for_update=True
            )
            if request is None or request.status is not VerificationStatus.PENDING:
                return None
            if request.created_at + self._verification_ttl <= now:
                request.status = VerificationStatus.EXPIRED
                request.updated_at = now
                return None
            sender = await session.get(
                Sender, request.sender_id, with_for_update=True
            )
            if sender is None:
                return None
            request.status = VerificationStatus.COMPLETED
            request.completed_at = now
            request.updated_at = now
            sender.verified = True
            sender.verified_at = now
            sender.updated_at = now

            needs = list(
                await session.scalars(
                    sa.select(Need)
                    .join(Report, Report.id == Need.source_report_id)
                    .where(
                        Report.sender_id == sender.id,
                        Need.verified.is_(False),
                    )
                )
            )
            for need in needs:
                need.verified = True
                need.updated_at = now

            cookie_token = new_opaque_token()
            token_hash = hash_token(cookie_token)
            csrf_token = derive_csrf_token(token_hash)
            expires_at = now + self._session_ttl
            auth_session = AuthSession(
                id=uuid4(),
                incident_id=request.incident_id,
                sender_id=sender.id,
                token_hash=token_hash,
                csrf_hash=hash_token(csrf_token),
                expires_at=expires_at,
                revoked_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(auth_session)
            await session.flush()

            await record_event(
                session,
                incident_id=request.incident_id,
                event_type="identity.verified",
                actor_type="sender",
                actor_id=sender.id,
                aggregate_type="sender",
                aggregate_id=sender.id,
                payload={
                    "sender_id": str(sender.id),
                    "verification_request_id": str(request.id),
                    "upgraded_needs": [str(need.id) for need in needs],
                },
            )
            await record_event(
                session,
                incident_id=request.incident_id,
                event_type="identity.session_created",
                actor_type="sender",
                actor_id=sender.id,
                aggregate_type="session",
                aggregate_id=auth_session.id,
                payload={"session_id": str(auth_session.id)},
            )
            return SessionGrant(
                session_id=auth_session.id,
                cookie_token=cookie_token,
                csrf_token=csrf_token,
                redirect_path=local_redirect_path(redirect_path),
                expires_at=expires_at,
            )

    async def _mark_failed(self, request_id: UUID) -> None:
        now = self._clock()
        async with transaction(self._session_factory) as session:
            request = await session.get(
                VerificationRequestRow, request_id, with_for_update=True
            )
            if request is not None and request.status is VerificationStatus.PENDING:
                request.status = VerificationStatus.FAILED
                request.send_claimed_at = None
                request.updated_at = now

    async def _load_request(self, request_id: UUID) -> VerificationRequestRow | None:
        async with self._session_factory() as session:
            return await session.get(VerificationRequestRow, request_id)

    def _provider_redirect(self, next_path: str) -> str:
        callback = self._settings.public_url("/auth/callback")
        safe_path = local_redirect_path(next_path)
        return f"{callback}?next={quote(safe_path, safe='/')}"


def _next_path(redirect_url: str | None) -> str:
    if not redirect_url:
        return "/"
    query = parse_qs(urlsplit(redirect_url).query)
    value = query.get("next", ["/"])[0]
    return local_redirect_path(value)


__all__ = ["IdentityError", "IdentityService", "SessionGrant"]

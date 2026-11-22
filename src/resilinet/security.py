"""Opaque session tokens, CSRF binding, cookie policy, and private headers.

Session cookies are random bearer values; only their SHA-256 digest is stored
in ``sessions.token_hash``. The CSRF token is derived from that digest with a
domain separator, so a cross-site request that cannot read the HttpOnly cookie
cannot produce it. Nothing here logs a token.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import unquote, urlsplit
from uuid import UUID

from .config import Settings

SESSION_COOKIE_NAME = "resilinet_session"
AUTH_NONCE_COOKIE = "resilinet_auth_nonce"
SESSION_TTL = timedelta(hours=8)
_CSRF_PREFIX = "resilinet-csrf-v1:"


@dataclass(frozen=True, slots=True)
class Principal:
    """The verified participant behind a server-side session."""

    session_id: UUID
    incident_id: UUID
    sender_id: UUID
    address: str
    expires_at: datetime
    csrf_token: str


def normalize_email(address: str) -> str:
    """Normalise an email address for identity comparison only."""
    return unicodedata.normalize("NFKC", address).strip().casefold()


def new_opaque_token() -> str:
    """A 32-byte URL-safe capability, never stored in plaintext."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """The stored form of a session or action token."""
    return hashlib.sha256(token.encode()).hexdigest()


def derive_csrf_token(session_token_hash: str) -> str:
    """Derive a CSRF token that only a holder of the cookie can compute."""
    return hashlib.sha256((_CSRF_PREFIX + session_token_hash).encode()).hexdigest()


def csrf_matches(expected: str, provided: str | None) -> bool:
    """Constant-time CSRF comparison; a missing token never matches."""
    if not provided:
        return False
    return hmac.compare_digest(expected, provided)


def session_cookie_secure(settings: Settings) -> bool:
    """``Secure`` cookies in live mode; plain HTTP is allowed for local tests."""
    return settings.is_live


def session_cookie_header(
    token: str,
    *,
    max_age_seconds: int,
    secure: bool,
) -> str:
    flags = [
        f"{SESSION_COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={max_age_seconds}",
    ]
    if secure:
        flags.append("Secure")
    return "; ".join(flags)


def clear_session_cookie_header(*, secure: bool) -> str:
    flags = [
        f"{SESSION_COOKIE_NAME}=",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        "Max-Age=0",
    ]
    if secure:
        flags.append("Secure")
    return "; ".join(flags)


def local_redirect_path(value: str | None, default: str = "/") -> str:
    """Accept only an internal absolute path; never an open redirect."""
    if not value:
        return default
    candidate = value.strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return default
    decoded = unquote(candidate)
    if (
        decoded.startswith("//")
        or "\\" in decoded
        or any(ord(character) < 0x20 for character in decoded)
    ):
        # Browsers normalize ``/\evil.com`` and control characters into a
        # cross-origin redirect even though urlsplit sees a local path.
        return default
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return default
    return candidate


def auth_nonce_cookie_header(nonce: str, *, secure: bool) -> str:
    flags = [
        f"{AUTH_NONCE_COOKIE}={nonce}",
        "Path=/auth",
        "HttpOnly",
        "SameSite=Lax",
        "Max-Age=600",
    ]
    if secure:
        flags.append("Secure")
    return "; ".join(flags)


def private_headers() -> dict[str, str]:
    """Headers for token-bearing and operator pages."""
    return {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        ),
    }


__all__ = [
    "AUTH_NONCE_COOKIE",
    "SESSION_COOKIE_NAME",
    "SESSION_TTL",
    "Principal",
    "auth_nonce_cookie_header",
    "clear_session_cookie_header",
    "csrf_matches",
    "derive_csrf_token",
    "hash_token",
    "local_redirect_path",
    "new_opaque_token",
    "normalize_email",
    "private_headers",
    "session_cookie_header",
    "session_cookie_secure",
]

"""Structured logging and request trace propagation.

Log records carry a UTC timestamp, a trace identifier and deliberately chosen
fields. Callers must not put raw email bodies, tokens, session values or full
addresses into log records.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOG_FIELDS: tuple[str, ...] = (
    "event",
    "incident_id",
    "message_id",
    "job_id",
    "outbox_id",
    "actor",
    "method",
    "path",
    "status_code",
    "duration_ms",
    "mode",
)

_current_trace_id: ContextVar[str | None] = ContextVar("resilinet_trace_id", default=None)


def current_trace_id() -> str | None:
    return _current_trace_id.get()


def set_trace_id(trace_id: str) -> Token[str | None]:
    return _current_trace_id.set(trace_id)


def reset_trace_id(token: Token[str | None]) -> None:
    _current_trace_id.reset(token)


def new_trace_id() -> str:
    return uuid.uuid4().hex


TOKEN_PATH_PREFIXES: tuple[str, ...] = ("/actions/",)


def redact_path(path: str) -> str:
    """Remove bearer tokens from paths before logging.

    ``/actions/{token}`` is a capability; logging it would leak the capability
    into operational logs. Query strings are never logged at all.
    """
    for prefix in TOKEN_PATH_PREFIXES:
        if path.startswith(prefix):
            return f"{prefix}{{token}}"
    return path


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with allow-listed extension fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        trace_id = getattr(record, "trace_id", None) or current_trace_id()
        if trace_id:
            payload["trace_id"] = trace_id
        for field in LOG_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Attach one JSON handler to the ``resilinet`` logger tree."""
    logger = logging.getLogger("resilinet")
    logger.setLevel(level.upper())
    logger.propagate = False
    install_access_log_redaction()
    for handler in logger.handlers:
        if getattr(handler, "_resilinet_json", False):
            handler.setLevel(level.upper())
            return
    handler = logging.StreamHandler()
    handler.setLevel(level.upper())
    handler.setFormatter(JsonFormatter())
    handler._resilinet_json = True  # type: ignore[attr-defined]
    logger.addHandler(handler)


class AccessLogFilter(logging.Filter):
    """Strip query strings and redact action tokens from uvicorn access logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            mutable = list(args)
            mutable[2] = redact_path(str(mutable[2]).split("?", 1)[0])
            record.args = tuple(mutable)
        return True


def install_access_log_redaction() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, AccessLogFilter) for item in access_logger.filters):
        access_logger.addFilter(AccessLogFilter())


class TraceIdMiddleware:
    """Assign a trace ID, expose it in a response header, and log the request.

    Only the path is logged; query strings can carry one-time tokens and are
    never written to logs.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.logger = logging.getLogger("resilinet.http")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        trace_id = new_trace_id()
        token = set_trace_id(trace_id)
        started = time.perf_counter()
        status_code = 500
        method = str(scope.get("method", "GET"))
        path = redact_path(str(scope.get("path", "")))

        async def send_with_trace(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = MutableHeaders(scope=message)
                headers["X-Trace-Id"] = trace_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
        finally:
            self.logger.info(
                "http_request",
                extra={
                    "trace_id": trace_id,
                    "method": method,
                    "path": path,
                    "status_code": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            reset_trace_id(token)

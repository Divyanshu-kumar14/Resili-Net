"""HTTP routes."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/api/v1/health", tags=["system"])
async def health() -> dict[str, str]:
    """Liveness only: process is up and serving requests."""
    return {"status": "ok"}

"""Redirect and cookie-boundary helpers."""

from __future__ import annotations

import pytest

from resilinet.security import local_redirect_path


@pytest.mark.parametrize(
    "candidate",
    [
        "//evil.example",
        "/\\evil.example",
        "/%2f%2fevil.example",
        "https://evil.example",
        "/ok\nSet-Cookie: sid=1",
        "/ok\r\nLocation: https://evil.example",
        "",
    ],
)
def test_local_redirect_path_rejects_cross_origin_and_control_forms(
    candidate: str,
) -> None:
    assert local_redirect_path(candidate) == "/"


def test_local_redirect_path_allows_internal_paths() -> None:
    assert local_redirect_path("/") == "/"
    assert local_redirect_path("/operator/reviews") == "/operator/reviews"
    assert local_redirect_path("/operator/reviews?tab=open") == (
        "/operator/reviews?tab=open"
    )

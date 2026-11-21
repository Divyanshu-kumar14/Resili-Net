"""Live sponsor contract gate (Task 12).

This test never runs in CI or local replay: it requires owner-provided live
credentials and an explicit opt-in. Its absence from a green run is NOT
evidence that the live flow works — see ``docs/evidence/phase2.md`` for the
unresolved gate. The Fake golden path in ``test_golden_path.py`` is the
local proof.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RESILINET_LIVE_CONTRACT") != "1",
    reason="live sponsor contract requires RESILINET_LIVE_CONTRACT=1 and "
    "owner-provided credentials (gate unresolved; see docs/evidence/phase2.md)",
)


def test_live_inbound_to_threaded_reply() -> None:
    required = [
        "AGENTBOXD_API_KEY",
        "AGENTBOXD_WEBHOOK_SECRET",
        "OPENAI_API_KEY",
        "MOJOAUTH_API_KEY",
    ]
    missing = [name for name in required if not os.environ.get(name)]
    assert not missing, f"missing live settings: {', '.join(missing)}"
    pytest.fail(
        "live harness not wired: provision the owner-approved test incident "
        "and implement the send/poll steps against real accounts before "
        "enabling this gate"
    )

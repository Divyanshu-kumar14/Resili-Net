"""Release rehearsal manifest (Phase 6, Task 27).

Asserts the evidence schema in ``docs/evidence/release-checklist.md``: sample
counts, privacy fields, metric thresholds, and command exit records. Live
gates may be honestly recorded as ``blocked`` with a reason; they must never
be manufactured as passes. A missing manifest, a threshold miss, or a blocked
entry without a reason fails this suite.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CHECKLIST = ROOT / "docs" / "evidence" / "release-checklist.md"


def _load_manifest() -> dict[str, Any]:
    text = CHECKLIST.read_text(encoding="utf-8")
    assert "# Release Checklist" in text, "checklist heading is missing"
    match = re.search(r"```json\n(.*?)```", text, re.DOTALL)
    assert match is not None, "fenced JSON manifest is missing"
    return json.loads(match.group(1))


def test_release_manifest_covers_corpus_and_replay() -> None:
    manifest = _load_manifest()
    assert manifest["corpus"]["messages"] == 30
    assert manifest["corpus"]["duplicate_pairs"] == 10
    replay = manifest["offline_replay"]
    assert replay["messages"] == 30
    assert replay["external_connection_attempts"] == 0
    assert replay["public_pages_status"] == [200, 200, 200]


def test_release_manifest_meets_metric_thresholds() -> None:
    manifest = _load_manifest()
    by_name = {metric["name"]: metric for metric in manifest["metrics"]}
    accuracy = by_name["extraction_field_accuracy"]
    assert accuracy["status"] == "pass"
    assert accuracy["value"] >= accuracy["threshold"] >= 90
    assert accuracy["sample_size"] == 80
    duplicates = by_name["duplicate_pairs"]
    assert duplicates["status"] == "pass"
    assert duplicates["value"] == duplicates["threshold"] == 10
    axe = by_name["axe_serious_critical"]
    assert axe["status"] == "pass" and axe["value"] == 0
    assert axe["sample_size"] >= 5
    lighthouse = by_name["lighthouse_accessibility"]
    assert lighthouse["status"] == "pass"
    assert lighthouse["value"] >= lighthouse["threshold"] >= 90


def test_release_manifest_privacy_scan_is_clean() -> None:
    manifest = _load_manifest()
    assert manifest["privacy"]["public_projection_findings"] == []
    assert manifest["privacy"]["verify_public_ok"] is True


def test_release_manifest_commands_are_green() -> None:
    manifest = _load_manifest()
    assert manifest["commands"], "command exit records are missing"
    for record in manifest["commands"]:
        assert record["exit"] == 0, record["command"]


def test_release_manifest_records_live_gates_honestly() -> None:
    manifest = _load_manifest()
    assert manifest["live"], "live evidence section is missing"
    for entry in manifest["live"]:
        assert entry["status"] in ("pass", "blocked"), entry["name"]
        if entry["status"] == "blocked":
            assert entry.get("reason"), entry["name"]
        if entry["status"] == "pass":
            assert entry.get("evidence"), entry["name"]
    assert any(entry["status"] == "blocked" for entry in manifest["live"]), (
        "live sponsor gates need credentials; a manifest with no blocked live "
        "entry is manufacturing evidence"
    )

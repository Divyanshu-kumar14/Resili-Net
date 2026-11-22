"""Task 20: demo corpus validation, replay completeness, duplicate benchmark.

The corpus is simulation data. These tests validate the fixtures themselves,
recompute every expected canonical quantity with the domain rules, check the
recorded replay extracts cover the corpus, run all ten duplicate pairs
through the duplicate domain rule, and push a representative subset through
the real durable pipeline.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resilinet.domain.duplicates import DuplicateKind, DuplicateNeedView, find_duplicate
from resilinet.domain.quantities import QuantityError, estimate_need, to_canonical
from resilinet.models import Need
from resilinet.schemas import NeedStatus, ParsedEmail, Resource

from .conftest import NOW, Clock
from .test_golden_path import build_harness, deliver

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"

CATEGORY_COUNTS = {
    "needs": 10,
    "supplies": 5,
    "status_cancel": 5,
    "ambiguous": 5,
    "near_duplicates": 3,
    "injections": 2,
}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE = re.compile(r"\b\d{10,}\b")


def load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def corpus() -> list[dict]:
    return load_json("messages.json")["messages"]


def message(message_id: str) -> dict:
    return next(item for item in corpus() if item["id"] == message_id)


def test_corpus_category_counts_ids_and_markers() -> None:
    messages = corpus()
    assert len(messages) == 30
    counts: dict[str, int] = {}
    for item in messages:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    assert counts == CATEGORY_COUNTS
    assert len({item["id"] for item in messages}) == 30
    assert len({item["marker"] for item in messages}) == 30
    for item in messages:
        assert item["marker"] in f"{item['subject']}\n{item['body']}"
        assert item["parse"]["context"]["kind"]
        assert item["expected"]["outcome"]


def test_corpus_and_demo_are_fictional_without_real_pii() -> None:
    demo = load_json("demo.json")
    assert demo["simulation"] is True
    assert load_json("messages.json")["simulation"] is True
    for item in corpus():
        assert item["sender"].endswith("@example.org"), item["id"]
        text = f"{item['subject']}\n{item['body']}"
        for address in EMAIL.findall(text):
            assert address.endswith("@example.org"), (item["id"], address)
        assert not PHONE.search(text), item["id"]


def test_demo_fixture_shape() -> None:
    demo = load_json("demo.json")
    kinds = [entity["type"] for entity in demo["entities"]]
    assert kinds.count("shelter") == 6
    assert kinds.count("depot") == 4
    assert demo["stock"], "demo needs varied seeded stock"
    roles = {member["role"] for member in demo["memberships"]}
    assert {"operator", "entity_coordinator", "depot_coordinator"} <= roles
    assert {need["ref"] for need in demo["needs"]} >= {"RN-000001", "RN-000002"}


def _canonical_expected(report: dict, context: dict) -> tuple[str, str, str]:
    resource = Resource(report["resource"])
    quantity = report.get("quantity")
    if quantity is None:
        estimate = estimate_need(resource, context.get("headcount"))
        assert estimate is not None, report
        return f"{estimate.amount:.3f}", estimate.unit.value, estimate.source.value
    try:
        canonical = to_canonical(resource, Decimal(str(quantity)), report.get("unit"))
    except QuantityError:
        # No canonical unit: the sender's unit is preserved, never converted.
        return f"{Decimal(str(quantity)):.3f}", str(report["unit"]).strip().casefold(), "stated"
    return f"{canonical.amount:.3f}", canonical.unit.value, canonical.source.value


def test_expected_quantities_match_domain_rules() -> None:
    checked = 0
    for item in corpus():
        expected_items = item["expected"].get("items") or []
        if not expected_items:
            continue
        parsed = ParsedEmail.model_validate(item["parse"])
        reports = {report.resource.value: report for report in parsed.reports}
        for expected in expected_items:
            report = reports[expected["resource"]]
            quantity, unit, source = _canonical_expected(
                report.model_dump(mode="json"), item["parse"]["context"]
            )
            assert expected["quantity"] == quantity, item["id"]
            assert expected["unit"] == unit, item["id"]
            assert expected["source"] == source, item["id"]
            checked += 1
    assert checked >= 15, checked


def test_replay_extracts_cover_the_corpus() -> None:
    extracts = load_json("replay/extracts.json")["extracts"]
    by_marker = {item["marker"]: item for item in corpus()}
    assert set(extracts) == set(by_marker)
    for marker, raw in extracts.items():
        recorded = ParsedEmail.model_validate(raw)
        declared = ParsedEmail.model_validate(by_marker[marker]["parse"])
        assert recorded.model_dump(mode="json") == declared.model_dump(mode="json"), marker


def _view(spec: dict, *, need_id: UUID, created_at) -> DuplicateNeedView:
    return DuplicateNeedView(
        need_id=need_id,
        entity_id=UUID(spec["entity_id"]) if spec.get("entity_id") else None,
        entity_name=spec["entity_name"],
        resource=spec["resource"],
        unit=spec["unit"],
        lat=Decimal(spec["lat"]) if spec.get("lat") is not None else None,
        lon=Decimal(spec["lon"]) if spec.get("lon") is not None else None,
        status=NeedStatus.OPEN,
        created_at=created_at,
    )


def test_duplicate_pairs_domain_decisions() -> None:
    pairs = load_json("duplicate_pairs.json")["pairs"]
    assert len(pairs) == 10
    assert {pair["expected"] for pair in pairs} == {"duplicate", "distinct"}
    for pair in pairs:
        first = _view(
            pair["first"],
            need_id=uuid4(),
            created_at=NOW - timedelta(hours=pair["first"].get("age_hours", 1)),
        )
        second = _view(pair["second"], need_id=uuid4(), created_at=NOW)
        decision = find_duplicate(second, [first], NOW)
        expected = (
            DuplicateKind.DUPLICATE
            if pair["expected"] == "duplicate"
            else DuplicateKind.NONE
        )
        assert decision.kind is expected, pair["id"]


async def _need_count(
    session_factory: async_sessionmaker[AsyncSession], incident_id: UUID
) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(
                sa.select(sa.func.count(Need.id)).where(Need.incident_id == incident_id)
            )
            or 0
        )


async def test_corpus_subset_flows_end_to_end(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    clock: Clock,
) -> None:
    harness = await build_harness(session_factory, incident_id, clock, fixtures={})
    chosen = [
        message("M01"),  # clean need, gallons -> litres
        message("M11"),  # clean supply
        message("M26"),  # near-duplicate of M01
        message("M29"),  # injection attempt
        message("M21"),  # ambiguous: must not create a need
    ]
    for item in chosen:
        harness.intelligence.set_fixture(
            item["marker"], ParsedEmail.model_validate(item["parse"])
        )
        await deliver(
            harness,
            provider_message_id=f"eval-{item['id'].lower()}",
            subject=item["subject"],
            text=item["body"],
            sender=item["sender"],
        )

    # One real need (M01); the near-duplicate corroborates it; the injection
    # and the ambiguous message create nothing.
    assert await _need_count(session_factory, incident_id) == 1
    async with session_factory() as session:
        need = await session.scalar(sa.select(Need).where(Need.incident_id == incident_id))
    assert need is not None
    assert f"{need.quantity:.3f}" == message("M01")["expected"]["items"][0]["quantity"]
    assert need.unit == message("M01")["expected"]["items"][0]["unit"]

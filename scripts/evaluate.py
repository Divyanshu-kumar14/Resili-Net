"""Run the 30-message simulation corpus through the durable pipeline.

Examples:

    uv run python scripts/evaluate.py
    uv run python scripts/evaluate.py --report /tmp/evaluation.json

Everything runs against a throwaway SQLite database with deterministic
fakes. Field-level accuracy compares persisted canonical quantities with
the corpus expectations; it measures canonicalisation and persistence
fidelity, NOT live-LLM extraction quality (that gate needs credentials).
The script exits non-zero when a target is missed: extraction accuracy
>= 90%, duplicate pairs 10/10 (domain rule), near-duplicate corroboration
3/3 (end to end).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from uuid import UUID, uuid4

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from seed_demo import seed_demo  # noqa: E402

from resilinet.adapters.fakes import (  # noqa: E402
    FakeGeocoder,
    FakeInboxProvider,
    FakeIntelligenceProvider,
)
from resilinet.db import create_engine, create_session_factory  # noqa: E402
from resilinet.domain.duplicates import (  # noqa: E402
    DuplicateKind,
    DuplicateNeedView,
    find_duplicate,
)
from resilinet.models import (  # noqa: E402
    Entity,
    JobType,
    Message,
    Need,
    OutboundEmail,
    PendingAction,
    PendingActionKind,
    ReviewCase,
    Supply,
)
from resilinet.ports import InboundEvent, RawMessage  # noqa: E402
from resilinet.schemas import NeedStatus, ParsedEmail  # noqa: E402
from resilinet.services.extraction import ExtractionService  # noqa: E402
from resilinet.services.ingestion import IngestionService  # noqa: E402
from resilinet.services.notifications import NotificationService, OutboxService  # noqa: E402
from resilinet.workers.handlers import MessageJobHandler  # noqa: E402
from resilinet.workers.runner import DurableWorker  # noqa: E402

NOW = datetime(2026, 11, 22, 9, 0, 0, tzinfo=UTC)
FIXTURES = ROOT / "fixtures"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def upgrade_database(path: Path) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, "head")


async def counts(session_factory, incident_id: UUID) -> dict[str, int]:
    async with session_factory() as session:
        async def count(model) -> int:
            return int(
                await session.scalar(
                    sa.select(sa.func.count(model.id)).where(
                        model.incident_id == incident_id
                    )
                )
                or 0
            )

        return {
            "needs": await count(Need),
            "supplies": await count(Supply),
            "pending": await count(PendingAction),
            "outbound": await count(OutboundEmail),
            "reviews": await count(ReviewCase),
        }


async def run(db_path: Path) -> dict:
    corpus = load("messages.json")["messages"]
    gazetteer = load("gazetteer.json")["places"]
    places = {
        place["name"]: (Decimal(place["lat"]), Decimal(place["lon"]))
        for place in gazetteer
    }

    engine = create_engine_for(db_path)
    session_factory = create_session_factory(engine)
    try:
        seeded = await seed_demo(session_factory, incident_key="demo-incident")
        async with session_factory() as session:
            from resilinet.models import Incident

            incident_id = await session.scalar(
                sa.select(Incident.id).where(Incident.key == "demo-incident")
            )
        assert incident_id is not None

        clock = Clock()
        inbox = FakeInboxProvider(webhook_secret="secret", now=clock)
        mailbox = await inbox.provision_inbox("demo-incident")
        intelligence = FakeIntelligenceProvider(
            {"bootstrap": ParsedEmail.model_validate(corpus[0]["parse"])}
        )
        extraction = ExtractionService(incident_id, intelligence, FakeGeocoder(places))
        handler = MessageJobHandler(
            session_factory=session_factory,
            inbox=inbox,
            extraction=extraction,
            notifications=NotificationService(),
            intelligence=intelligence,
            identity=None,
            public_base_url="http://testserver",
            clock=clock,
        )
        worker = DurableWorker(
            session_factory,
            {JobType.PROCESS_MESSAGE: handler, JobType.FETCH_CONTENT: handler},
            clock=clock,
            jitter=lambda: 0.0,
            worker_id="evaluation-worker",
        )
        ingestion = IngestionService(incident_id, session_factory, clock=clock)
        outbox = OutboxService(session_factory, inbox, clock=clock)

        fields_correct = 0
        fields_total = 0
        outcomes: dict[str, dict[str, int]] = {}
        failures: list[str] = []
        durations_ms: list[float] = []

        def record(category: str, ok: bool, label: str) -> None:
            bucket = outcomes.setdefault(category, {"ok": 0, "total": 0})
            bucket["total"] += 1
            if ok:
                bucket["ok"] += 1
            else:
                failures.append(label)

        for item in corpus:
            marker = item["marker"]
            intelligence.set_fixture(
                marker, ParsedEmail.model_validate(item["parse"])
            )
            provider_id = f"eval-{item['id'].lower()}"
            before = await counts(session_factory, incident_id)
            async with session_factory() as session:
                needs_before = set(
                    await session.scalars(
                        sa.select(Need.id).where(Need.incident_id == incident_id)
                    )
                )
            started = time.perf_counter()
            inbox.deliver_inbound(
                mailbox_id=mailbox.id,
                provider_message_id=provider_id,
                sender=item["sender"],
                subject=item["subject"],
                text=item["body"],
                thread_id=f"thread-{provider_id}",
                received_at=NOW,
            )
            event = InboundEvent(
                event_id=f"evt-{provider_id}",
                event_type="message.received",
                mailbox_id=mailbox.id,
                provider_message_id=provider_id,
                sender=item["sender"],
                subject=item["subject"],
                received_at=NOW,
                withheld=False,
                mailbox_address="incident@example.org",
                thread_id=f"thread-{provider_id}",
                raw=RawMessage(
                    provider_message_id=provider_id,
                    mailbox_id=mailbox.id,
                    sender=item["sender"],
                    subject=item["subject"],
                    text=item["body"],
                    received_at=NOW,
                    thread_id=f"thread-{provider_id}",
                ),
            )
            await ingestion.accept(event)
            for _ in range(25):
                if await worker.run_once() == 0:
                    break
            while await outbox.deliver_due():
                pass
            durations_ms.append((time.perf_counter() - started) * 1000)
            after = await counts(session_factory, incident_id)
            expected = item["expected"]
            outcome = expected["outcome"]
            category = item["category"]

            if outcome in {"need", "supply"}:
                ok_items = await score_items(
                    session_factory, incident_id, provider_id, item, needs_before
                )
                for ok_fields, total_fields in ok_items:
                    fields_correct += ok_fields
                    fields_total += total_fields
                record(
                    category,
                    all(ok == total for ok, total in ok_items)
                    and len(ok_items) == len(expected["items"]),
                    item["id"],
                )
            elif outcome == "duplicate":
                record(
                    category,
                    after["needs"] == before["needs"]
                    and after["outbound"] > before["outbound"],
                    item["id"],
                )
            elif outcome == "clarification":
                record(
                    category,
                    after["needs"] == before["needs"]
                    and (
                        after["outbound"] > before["outbound"]
                        or after["reviews"] > before["reviews"]
                    ),
                    item["id"],
                )
            elif outcome == "pending_action":
                found, detail = await pending_matches(
                    session_factory, provider_id, expected["pending_kind"]
                )
                record(category, found, f"{item['id']}: {detail}")
            elif outcome == "query_reply":
                record(
                    category,
                    after["outbound"] > before["outbound"]
                    and after["needs"] == before["needs"]
                    and after["pending"] == before["pending"],
                    item["id"],
                )
            elif outcome == "no_mutation":
                record(
                    category,
                    after["needs"] == before["needs"]
                    and after["supplies"] == before["supplies"]
                    and after["pending"] == before["pending"],
                    item["id"],
                )
            else:  # pragma: no cover - fixture validation guards this
                record(category, False, f"{item['id']}: unknown outcome")

        pairs = load("duplicate_pairs.json")["pairs"]
        pair_correct = 0
        for pair in pairs:
            first = pair_view(pair["first"], NOW - timedelta(hours=pair["first"].get("age_hours", 1)))
            second = pair_view(pair["second"], NOW)
            decision = find_duplicate(second, [first], NOW)
            wanted = (
                DuplicateKind.DUPLICATE
                if pair["expected"] == "duplicate"
                else DuplicateKind.NONE
            )
            if decision.kind is wanted:
                pair_correct += 1
            else:
                failures.append(pair["id"])
    finally:
        await engine.dispose()

    accuracy = (fields_correct / fields_total) if fields_total else 0.0
    report = {
        "seeded": seeded,
        "extraction": {
            "fields_correct": fields_correct,
            "fields_total": fields_total,
            "accuracy": round(accuracy, 4),
            "target": 0.9,
        },
        "duplicate_pairs_domain": {"correct": pair_correct, "total": len(pairs)},
        "outcomes": outcomes,
        "failures": failures,
        "pipeline_ms_p50": round(median(durations_ms), 1) if durations_ms else None,
        "note": "Deterministic fakes: accuracy measures canonicalisation and "
        "persistence fidelity, not live-LLM extraction quality.",
    }
    report["pass"] = (
        accuracy >= 0.9
        and pair_correct == len(pairs)
        and all(bucket["ok"] == bucket["total"] for bucket in outcomes.values())
    )
    return report


def create_engine_for(db_path: Path):
    from resilinet.config import Settings

    settings = Settings(
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{db_path}",
        incident_key="demo-incident",
        _env_file=None,
    )
    return create_engine(settings)


def pair_view(spec: dict, created_at: datetime) -> DuplicateNeedView:
    return DuplicateNeedView(
        need_id=uuid4(),
        entity_id=UUID(spec["entity_id"]) if spec.get("entity_id") else None,
        entity_name=spec["entity_name"],
        resource=spec["resource"],
        unit=spec["unit"],
        lat=Decimal(spec["lat"]) if spec.get("lat") is not None else None,
        lon=Decimal(spec["lon"]) if spec.get("lon") is not None else None,
        status=NeedStatus.OPEN,
        created_at=created_at,
    )


async def pending_matches(session_factory, provider_id: str, kind: str) -> tuple[bool, str]:
    async with session_factory() as session:
        message = await session.scalar(
            sa.select(Message).where(Message.provider_message_id == provider_id)
        )
        if message is None:
            return False, "message row missing"
        kinds = list(
            await session.scalars(
                sa.select(PendingAction.kind).where(
                    PendingAction.source_message_id == message.id
                )
            )
        )
        found = PendingActionKind(kind) in kinds
        return found, f"expected {kind}, found {[k.value for k in kinds]}"


async def score_items(session_factory, incident_id, provider_id, item, needs_before=frozenset()) -> list[tuple[int, int]]:
    """Score expected items field by field: entity, resource, quantity, unit, source."""
    expected = item["expected"]
    results: list[tuple[int, int]] = []
    async with session_factory() as session:
        if expected["outcome"] == "need":
            rows = list(
                await session.execute(
                    sa.select(Need, Entity.name)
                    .join(Entity, Need.entity_id == Entity.id)
                    .where(
                        Need.incident_id == incident_id,
                        Need.id.not_in(needs_before) if needs_before else Need.id.is_not(None),
                    )
                )
            )
            pool = [
                {
                    "entity": name,
                    "resource": need.resource.value,
                    "quantity": need.quantity,
                    "unit": need.unit,
                    "source": need.quantity_source.value,
                }
                for need, name in rows
            ]
        else:
            message = await session.scalar(
                sa.select(Message).where(Message.provider_message_id == provider_id)
            )
            pendings = list(
                await session.scalars(
                    sa.select(PendingAction).where(
                        PendingAction.source_message_id == message.id,
                        PendingAction.kind == PendingActionKind.UPDATE_SUPPLY,
                    )
                )
            )
            pool = []
            for pending in pendings:
                entity = await session.get(Entity, pending.entity_id)
                pool.append(
                    {
                        "entity": entity.name if entity else "",
                        "resource": pending.payload_json.get("resource"),
                        "quantity": Decimal(str(pending.payload_json.get("quantity"))),
                        "unit": pending.payload_json.get("unit"),
                        "source": "stated",
                    }
                )
    for wanted in expected["items"]:
        match = next(
            (
                row
                for row in pool
                if row["entity"] == wanted["entity"]
                and row["resource"] == wanted["resource"]
            ),
            None,
        )
        if match is None:
            results.append((0, 5))
            continue
        correct = 2  # entity + resource already matched
        if Decimal(match["quantity"]) == Decimal(wanted["quantity"]):
            correct += 1
        if match["unit"] == wanted["unit"]:
            correct += 1
        if match["source"] == wanted["source"]:
            correct += 1
        results.append((correct, 5))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the ResiliNet demo corpus.")
    parser.add_argument("--report", default=None, help="also write the JSON report here")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "evaluate.db"
        upgrade_database(db_path)  # sync: alembic env owns its own event loop
        report = asyncio.run(run(db_path))
    text = json.dumps(report, indent=2)
    print(text)
    if args.report:
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())

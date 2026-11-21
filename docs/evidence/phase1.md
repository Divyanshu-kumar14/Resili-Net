# Phase 1 Evidence — Durable Intake, Extraction, and Clarification

Status: Tasks 4–7 implemented and locally verified on 2026-11-22. No live
sponsor account was used; every network-dependent claim below is marked.

Environment: Python 3.11.16, SQLite 3.53.1, `uv` with the frozen `uv.lock`
(48 packages). Application code is Python 3.11, FastAPI, SQLAlchemy 2 async,
Pydantic 2, Alembic 1.20.1, OpenAI Python SDK 2.54.0.

## Commands and results

| Command | Result |
|---|---|
| `uv run pytest -q` | **180 passed** |
| `uv run ruff check src tests` | **All checks passed** |
| `uv run mypy src` | **Success: no issues found in 31 source files** |
| `RESILINET_DATABASE_URL=… uv run alembic upgrade head` | upgrade `0001` applied to a scratch SQLite database |
| `RESILINET_DATABASE_URL=… uv run alembic check` | **No new upgrade operations detected** |
| `RESILINET_DATABASE_URL=… uv run alembic downgrade base` | clean downgrade to an empty schema |
| `osv-scanner scan source -r .` (v2.7.1) | scanned `uv.lock`, 48 packages, **No issues found** |

Targeted Phase 1 suites (96 tests):

```text
tests/unit/test_extraction.py                  35 passed
tests/integration/test_ingestion.py            18 passed
tests/integration/test_geocoding.py            13 passed
tests/integration/test_clarification.py        18 passed
tests/integration/test_durable_pipeline.py      6 passed
tests/integration/test_webhook.py               6 passed
```

## Task 4 — relational model, migration, durable intake

Verified behaviours:

- `alembic upgrade head` creates the 18 aggregates plus `reference_counters`
  and `alembic_version`; the migration round-trips and matches the model.
- A repeated provider event returns the original message ID and creates no
  second message or job; the same provider ID in a second incident is
  independent.
- A withheld envelope (even one carrying a partial snippet) is stored as
  `deferred` with a `fetch_content` job; the worker fetches the body, refuses to
  process it while the provider still marks it withheld, normalizes it, marks
  the message `queued`, and enqueues exactly one `process_message` job
  (`dedupe_key = process:<message>`).
- Terminal job writes are fenced on `(status=leased, lease_owner, attempts)`:
  a worker whose lease expired cannot mark another worker's job succeeded,
  retried, or dead-lettered. A deliberate stale-claim test proves the write is
  discarded.
- Quote blocks (`>` and `On … wrote:`) and signatures (`-- ` / mobile signature)
  are stripped; NFKC and whitespace normalization apply; a message consisting
  only of quoted text keeps its content rather than becoming empty.
- Sender identity is case-insensitive (`Volunteer@Example.ORG` →
  `volunteer@example.org`).
- Rolling limits are persisted rows: 20/sender/hour and 200/incident/hour.
  Intake locks the incident row (`SELECT … FOR UPDATE`) before counting, so the
  windows are serialized on PostgreSQL instead of racing. The limit means
  "this many are allowed"; the next message is throttled and recorded with the
  limit and scope. At most one notice per sender per window is queued
  (`outbox:<uuid>`), which bounds backscatter to a spoofed address. An
  incident-level limit produces an incident-scoped notice.
- Attachments are ignored with an audit event; no attachment content is read.
- A job whose lease expired is reclaimed and its attempt counter increments; a
  live lease is not stolen. A transient failure schedules bounded backoff and
  a third attempt dead-letters with an event.
- Public reference codes come from a locked `reference_counters` row with a
  random six-digit start; `RN-424242`, `RN-424243` and a distinct `AL-` code
  were allocated and parsed back.
- `POST /internal/agentboxd/events` verifies the raw body HMAC and a 300-second
  timestamp window before JSON parsing. Signed delivery returns 204 twice with
  one message and one job; tampered, unsigned, and stale requests return 400;
  an unconfigured webhook returns 503; an unprovisioned incident returns 404.

## Task 5 — strict extraction and OpenAI adapter

Verified behaviours (35 tests, all local):

- Negative, NaN, and infinite quantities, out-of-range coordinates,
  out-of-range confidence values, unknown resources, and extra fields are
  rejected by the schema.
- `ParsedEmail.split_required` defaults to false and marks unattributable
  compound messages.
- Five prompt-injection pattern families are detected
  (`instruction_override`, `bulk_release`, `schema_override`,
  `tool_invocation`, `role_override`); benign relief text is not flagged.
  The audit finding carries pattern names, a trace ID, and a message reference,
  never the matched text, and the parsed result is unchanged.
- Location confidence below 0.60, item confidence below 0.75, a missing
  entity, an unlocatable report, a quantity above 1,000,000, and
  `split_required` each request clarification; the partial extraction is
  retained for a later merge.
- Explicit coordinates bypass the geocoder and are labelled `reported`/`exact`.
- The adapter calls `responses.parse` with `text_format=ParsedEmail`,
  temperature 0, no tools, and an `<email>`-delimited user message. A missing
  `output_parsed` or a schema `ValidationError` is repaired exactly once with
  validation feedback, then raises `ExtractionError`. Provider outages surface
  as `ProviderUnavailableError`; critique failures raise `CritiqueError`.
- `tests/fixtures/openai/extraction_need.contract.json` is a hand-written
  schema contract example. **It is not a recorded live response**; no OpenAI
  account was available.

## Task 6 — gazetteer-first geocoding and report persistence

Verified behaviours (12 tests, all network mocked):

- `fixtures/gazetteer.json` contains 20 fictional places labelled
  `simulation: true`; Gandhi Park resolves to 22.572600/88.363900 as `exact`
  with zero HTTP requests. The gazetteer is consulted **before** the cache and
  is not cached itself, so a previously cached negative/`area` result can never
  mask a place that the curated list can resolve exactly.
- Unicode and whitespace variants of a place normalize to one cache key
  (`gandhi park`).
- Unknown places return `none`; replay/disabled mode returns `none` with no
  cache write and no request.
- A Nominatim result is cached as `area` per `(incident, normalized place)`;
  the second lookup performs no request. Empty provider results are negatively
  cached; timeouts and malformed payloads are **not** cached so a retry is
  possible.
- Every request carries `ResiliNet/0.1 (contact)` and `format=jsonv2`, even
  when a custom HTTP client is injected.
- Reports and entities persist with `lat`/`lon` null when no location is
  known; a later report with coordinates enriches the same entity rather than
  creating a second one.
- Persistence creates no `Need`, `Supply`, or `Allocation`.
- **Not yet proven here:** "list-visible but cannot produce a distance match"
  requires the public list API (Task 17) and matching rules (Task 9). Phase 1
  proves the no-coordinate record is persisted with null coordinates.

## Task 7 — compound reports and one-round clarification

Verified behaviours (12 tests plus 6 end-to-end pipeline tests):

- A compound supply message with two resources for one depot is ready and
  persists two reports under one entity.
- Missing location, or missing quantity plus headcount, produces exactly one
  question listing every missing field; the message moves to
  `clarification_pending` with the partial extraction stored.
- A missing quantity with a known headcount is estimable and asks no question.
- A mixed entity/location/kind message asks the sender to split it; no report
  or entity is created.
- A correction from the original sender merges only the requested fields,
  keeps unrequested authoritative values, persists the completed report, marks
  both messages processed, and queues a merge acknowledgement.
- A second ambiguity, a second linked correction, or a correction from a
  different sender creates a `ReviewCase` and a sender-visible review notice.
  A wrong-sender reply leaves the original report `clarification_pending` and
  notifies the sender who actually replied.
- State guards prevent regression: a correction that arrives after the report
  was completed or escalated becomes a `clarification.late_correction` review
  and never rewrites the original; `process_ready` refuses any message that is
  not `queued` or `clarification_pending`.
- A correction that is still blocked on a requested field, is implausibly large
  (over 1,000,000), or carries low-confidence location/quantity/headcount is
  escalated rather than merged. Model field names such as `items` are mapped to
  `quantity` so the question is answerable and mergeable.
- A `QUERY`/`OTHER` message receives a read-only status explanation; no report,
  need, supply, allocation, or stock change is created.
- An injection message is audited as `security.extraction_injection` and the
  pipeline creates no need, supply, allocation, or outbound email.
- Clarification and pipeline tests assert every clarification outcome queues an
  outbox row. A clean NEED/SUPPLY deliberately queues **no** acknowledgement:
  the threaded recommendation is Task 12's single logical notification.

## Deliberate design notes

- **Reference allocator:** a `reference_counters` table with a row lock and a
  random six-digit start replaces a PostgreSQL-only sequence. It works on
  SQLite and PostgreSQL and returns a number on rollback instead of burning it.
- **Schema additions:** `ParsedEmail.split_required` expresses a model that
  cannot attribute a compound message to one context;
  `ClarificationRequired.parsed` retains the partial extraction so one-round
  merging does not re-interpret the email from scratch.
- **New modules:** `services/events.py` (append-only audit helper) and
  `workers/handlers.py` (durable message handlers). The plan's file list is a
  proposal; the interfaces stay as specified.
- **Worker supervision:** `DurableWorker.run_once()` is implemented and tested,
  but the application does not start a supervised loop yet. That is Task 25's
  deployment concern; Phase 1 proves the durable job state and recovery.
- **Outbox recovery:** a row left in `sending` by a crash is reclaimed after a
  stale window; the stable idempotency key makes the replay safe. A claimed row
  that exhausted its attempts becomes `failed` with an audit event.
- **SQLite concurrency:** `SKIP LOCKED` is a no-op on SQLite, which serializes
  writers; local development runs one consumer. PostgreSQL provides the real
  concurrent claim guarantee, proven by Task 22.
- **Residual injection boundary:** the email is delimited and tag characters are
  escaped, but this is prompt-level hardening, not a formal sandbox. The model
  has no tools and its output is schema-validated, so an injection can at most
  influence the reported content, never the control flow.
- **Phase 0 slice:** the in-memory ingestion store is now
  `MemoryIngestionService`, used only by the synchronous Phase 0 pipeline that
  Phase 2 replaces. It is not a second durable engine.

## Unresolved gates

| Gate | Why it is unresolved |
|---|---|
| Live OpenAI structured extraction | No owner-provided API key or quota. Adapter contract is proven with a stub and a schema fixture only. |
| Live Nominatim policy/behaviour | No outbound call was made. UA, timeout, malformed/empty handling and caching are proven with a mock transport. |
| Live Agentboxd inbox + HMAC webhook | No owner-provided credentials. The signed-envelope path is proven against the Fake and the shared parser. Account-only capabilities remain as recorded in `docs/evidence/phase0.md`. |
| Reply-to-map latency, Momen map | Out of Phase 1 scope. |

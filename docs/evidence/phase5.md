# Phase 5 Evidence — Reliability, Metrics, Accessibility, and Replay Proof

Status: Tasks 20–24 implemented and locally verified on 2026-11-22. All
provider traffic in this phase is deterministic fakes or a disposable local
PostgreSQL; no live Agentboxd, OpenAI/NVIDIA, MojoAuth, or Momen account was
used, and no live sponsor result is claimed. The Render production database
was never touched by the concurrency suite.

Environment: Python 3.11.16, SQLite 3.53.1, PostgreSQL 18.7 (local Docker,
`docker-compose.test.yml`), Node 26.13.1, pnpm 10.28.1, Playwright 1.65.1,
axe-core 4.14.1, Lighthouse 12.8.2. New Python dependencies: none.
New Node devDependencies (audit tooling only): `playwright`, `axe-core`,
`lighthouse`, `chrome-launcher`.

## Commands and results

| Command | Result |
|---|---|
| `uv run pytest -q` | **466 passed, 1 skipped** (the pre-existing live-contract gate) |
| `uv run ruff check src scripts tests` | **All checks passed** |
| `uv run mypy src` | **Success: no issues found in 50 source files** |
| `uv run python scripts/seed_demo.py` (twice, scratch SQLite) | stable counts: 10 entities, 7 stock, 2 needs |
| `uv run python scripts/evaluate.py` | **pass**: 80/80 fields (100%), duplicate pairs 10/10, outcomes 30/30, p50 20.2 ms/message |
| `uv run pytest tests/postgres -q` (×10 runs) | **4 passed every run** |
| `uv run python scripts/replay_demo.py` | 30 messages, p50 22.7 ms, pages 200/200/200, **0 external connection attempts**, exit 0 |
| `pnpm run audit` | **PASSED**: axe zero serious/critical on 5 pages, Lighthouse accessibility **100** on `/replay` and `/replay/lite` |
| `osv-scanner scan source --lockfile uv.lock` | **No issues found** |
| `osv-scanner scan source --lockfile pnpm-lock.yaml` | 14 findings, all inside Lighthouse's transitive tooling tree (see below) |

Baseline before this phase was 444 passed, 1 skipped (445 total). The suite
is now 467 total (+7 evaluation, +9 worker recovery, +2 offline replay,
+4 PostgreSQL concurrency).

## Task 20 — corpus, seed, evaluation

- `fixtures/messages.json`: exactly 30 fictional messages (10 clean needs,
  5 supplies, 5 status/cancel, 5 ambiguous, 3 near-duplicates, 2
  injections), all senders at `example.org`, places from the existing
  20-place fictional gazetteer. `fixtures/demo.json` seeds one simulation
  incident, 6 shelters, 4 depots, varied stock, 3 unverified memberships.
- `tests/e2e/test_evaluation.py` recomputes every expected canonical
  quantity with the domain rules (gallons→litres, headcount estimates,
  preserved units), validates replay-extract completeness for all 30
  markers, runs all 10 duplicate pairs through `find_duplicate`, and pushes
  a 5-message subset through the real pipeline.
- First evaluation run exposed two fixture defects, fixed in the fixtures
  (not by weakening expectations): seeded demo needs collided with corpus
  needs M01/M02 and correctly absorbed them as duplicates; two stock
  corrections used `STATUS_UPDATE` where the taxonomy routes stock
  snapshots as `SUPPLY`.
- Accuracy semantics: with deterministic fakes this measures
  canonicalisation and persistence fidelity, **not** live-LLM extraction
  quality. That gate still needs credentials.

## Task 21 — retries, dead letters, deadline acks, readiness

- Typed classification in the runner: `PermanentJobError`, schema-invalid
  `ExtractionError`/`CritiqueError`, and non-retryable
  `ProviderUnavailableError` dead-letter immediately; everything else
  retries with bounded exponential backoff (max 3 attempts).
- Every terminal failure now escalates a `DEAD_LETTER` review case and
  queues exactly one truthful sender notice before the dead letter is
  recorded (`tests/integration/test_worker_recovery.py`, 9 tests).
- A 15 s deadline-ack job is scheduled per accepted message (payload-flagged
  process job, unique dedupe key). It sends exactly one acknowledgement
  only while the message is still unprocessed and no reply exists; a
  finished message never receives one. Existing job-count assertions in the
  ingestion/webhook/pipeline suites were updated to the process+ack
  contract; the exactly-one-process-job dedupe invariant is still asserted.
- Leases are heartbeat-extended (fenced like terminal writes) while a
  handler runs; all terminal job events carry `duration_ms` and contain no
  email addresses or message bodies (asserted).
- `GET /api/v1/ready` reports database reachability plus in-app worker
  heartbeat freshness (≤90 s); external-worker deployments report the
  worker as `external`. Crash-after-registration resume rebuilds matching
  with exactly one need and one reservation (tested).

## Task 22 — PostgreSQL concurrency proof

- `tests/postgres/` runs against a disposable local PostgreSQL 18.7
  (`docker-compose.test.yml`, port 54329). Each run creates a uniquely
  named `resilinet_scratch_*` database, migrates it, and drops it after
  verifying the name prefix. It skips cleanly when PostgreSQL is absent.
- The suite **found a real race**: a cancellation and a delivery against
  the same allocation could both succeed. `AllocationService._locked_trio`
  peeked the allocation unlocked, and its `FOR UPDATE` re-read returned
  the stale identity-map snapshot, so the state check ran on pre-lock
  state. Locked re-reads in `_locked_need`/`_locked_trio` now use
  `populate_existing`; the pattern is documented in
  `docs/architecture.md`. Commit and snapshot races already held.
- Post-fix: 4/4 tests across 10 consecutive runs (two-worker commit
  capping, snapshot-vs-reservation, same-grant double delivery consumed
  exactly once, cancel-vs-delivery XOR). No sleeps anywhere; tasks align
  on an Event barrier. `.github/workflows/test.yml` runs frozen install,
  ruff, mypy, and the full suite against a PostgreSQL 18 service — it has
  not yet run on GitHub; the first push exercises it.

## Task 23 — network-disabled replay

- Replay mode now refuses live provider construction even when keys are
  present: composition returns the recorded-extraction fake, a fake inbox,
  and no auth provider (`tests/e2e/test_offline_replay.py`). A socket
  guard records and blocks every non-loopback connection attempt; the
  need/supply/duplicate/injection/medical scenarios and the
  map/table/lite reads complete with the guard empty.
- `scripts/replay_demo.py` replays all 30 corpus messages through the
  app factory: pages 200, zero external attempts, p50 22.7 ms/message.
  That is local pipeline timing; live sponsor latency is unmeasured and
  unclaimed. `docs/demo-script.md` is the presenter runbook.

## Task 24 — accessibility audit

- `pnpm run audit` (`scripts/verify_accessibility.mjs`) boots the real app
  against a seeded throwaway database (test-only sessions and an action
  link from `scripts/seed_audit.py`, fake providers from
  `scripts/run_audit_server.py`) and runs: axe (WCAG 2.1 AA tags) on
  `/replay`, `/replay/lite`, `/operator/login`, `/operator/reviews`, and
  an action form; a keyboard walk (filters → submit → marker → Enter
  jumps to the row); a 640 px narrow-viewport overflow check; and
  Lighthouse accessibility on both public pages. A deliberately
  inaccessible fixture proves the checker can fail (5 violations found).
- Verdicts: **axe zero serious/critical on all five pages; Lighthouse
  100/100**; keyboard and viewport checks green. Two real defects were
  found and fixed: the map SVG carried `role="img"` around focusable
  marker links (now `role="group"`), and the audit itself initially
  landed on a 503 JSON error because action pages correctly refuse to
  serve without an auth provider (audit factory injects the fake).
  The app CSP (`script-src 'self'`) blocked axe injection during setup;
  the audit uses Playwright's context-level `bypassCSP` — the application
  was not weakened.
- `osv-scanner` on `pnpm-lock.yaml` reports 14 findings, all inside
  Lighthouse's transitive dependency tree (Chromium download/build-time
  tooling such as `extract-zip`, `minimatch`, `zod`). Disposition:
  accepted for local audit tooling only — devDependencies, never shipped,
  never exposed to untrusted input; `pnpm update` did not move the pinned
  transitive versions. The Python lockfile scan is clean.
- Not performed: VoiceOver/NVDA screen-reader pass (unavailable on this
  Linux host) and the published-Momen page audit (account gate, Task 19).
  Both remain open below; no score is claimed for them.

## Unresolved gates

| Gate | Why it is unresolved |
|---|---|
| Live sponsor latency (email → reply → map) | No Agentboxd/OpenAI/NVIDIA credentials; replay p50 (22.7 ms) is local-only and is never presented as sponsor latency. |
| Published Momen map/list + `/lite` audit and ≤5 s gate | No Momen editor account (work-email wall; institute email is the route). Task 19 remains blocked; Phase 4 exit gate still unmet. |
| Live MojoAuth magic-link login | The deployed service still carries the placeholder key; a real key (institute-email signup) unblocks it. |
| Screen-reader (VoiceOver/NVDA) manual pass | No screen reader available on this host; automated axe + keyboard coverage stands in until then. |
| GitHub Actions first run | `.github/workflows/test.yml` is new and unexercised until the next push. |
| Phase 5 commits | Per-task commits from the plan (`test: add reproducible relief corpus…`, `feat: recover relief pipelines…`, `test: prove stock invariants…`, `test: verify complete network-independent replay…`, `feat: meet accessible relief map…`) are prepared but not executed; awaiting the user's go-ahead. |

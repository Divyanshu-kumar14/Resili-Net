# Phase 2 Evidence — Matching, Critic, Reservations, and Replies

Status: Tasks 8–12 implemented and locally verified on 2026-11-22. No live
sponsor account was used; every network-dependent claim below is marked.

Environment: Python 3.11.16, SQLite 3.53.1, `uv` with the frozen `uv.lock`
(48 packages, unchanged in this phase — no new dependencies).

## Commands and results

| Command | Result |
|---|---|
| `uv run pytest -q` | **288 passed, 1 skipped** (live contract gate, see below; includes the review-response tests in the addendum) |
| `uv run ruff check src tests` | **All checks passed** |
| `uv run mypy src` | **Success: no issues found in 37 source files** |
| `RESILINET_DATABASE_URL=… uv run alembic upgrade head` | upgrades `0001 → 0002` applied to a scratch SQLite database |
| `RESILINET_DATABASE_URL=… uv run alembic check` | **No new upgrade operations detected** |
| `RESILINET_DATABASE_URL=… uv run alembic downgrade base` | clean downgrade through `0002 → 0001 → base` |
| `osv-scanner scan source -r .` (v2.7.1) | scanned `uv.lock`, 48 packages, **No issues found** |

Targeted Phase 2 suites (116 tests):

```text
tests/unit/test_quantities.py                  23 passed
tests/unit/test_priority.py                     7 passed
tests/unit/test_duplicates.py                   9 passed
tests/unit/test_matching.py                    11 passed
tests/unit/test_lifecycle.py                   20 passed
tests/unit/test_critic_rules.py                10 passed
tests/integration/test_reservations.py         15 passed
tests/integration/test_critic_pipeline.py       7 passed
tests/integration/test_outbox.py                7 passed
tests/e2e/test_golden_path.py                   7 passed
```

## Task 8 — canonical quantities, estimates, safety caps

- `300 gallons = 1135.500 l`; 40 people → estimated 120 l water / 80 meals /
  40 blankets; unsupported resources return no estimate.
- Zero SUPPLY is a valid empty snapshot; zero NEED is treated as missing and
  estimated from headcount or clarified (clarification layer, Phase 1).
- Negative, NaN, infinite, unknown-unit, sub-resolution, and over-cap values
  (`>10,000 l` water, `>1,000,000` overall) raise `QuantityError`; nothing is
  ever clamped. Amounts quantize to 0.001 with Decimal ROUND_HALF_EVEN,
  pinned by tests.
- New: `canonical_unit()` and `to_stated()` — resources without a canonical
  unit keep their stated unit (normalized, never converted); matching only
  compares equal resource+unit pairs. Stated raw quantity/unit remain on the
  persisted `Report` rows; `Need.source_report_id` links the commitment to
  its evidence.

## Task 9 — priority, duplicates, deterministic scoring

- Priority `urgency·10 + min(headcount,200)/10 + age_hours·0.5 + medical 20`;
  order priority DESC, verified DESC (tie-break only), created_at ASC,
  id ASC. Boundary tests: 0/50/>50 km, freshness flat to 6 h then linear to
  0 at 24 h, headcount cap, tie-break determinism.
- Duplicates: same entity or (name similarity ≥0.85 within 150 m), open need
  <12 h. Two distinct shelters 100 m apart produce `REVIEW_CANDIDATE`, never
  a merge; 12 h/13 h, closed, and different-resource cases are `NONE`.
- `build_drafts` greedily splits by descending score over a shared pool; a
  200-case generated property test proves per-supply and per-need totals
  never exceed availability/remaining.

## Task 10 — transactional reservations and reconciliation

- `AllocationService` (durable) commits with need/supply row locks in sorted
  ID order, recomputes remaining/availability from committed rows, caps
  stale drafts to fresh availability, and rejects stale candidates whose
  resource/unit no longer matches. Committing twice reserves once.
- Snapshots are absolute. A lowered snapshot or reduced need cancels only
  the newest excess (proposals first), never delivered history; requesters
  of cancelled allocations are notified. `30 delivered of 100, then cancel →
  70 remaining`; `fulfilled 100 → revised 150 → exactly 50 remaining`; any
  revision below `delivered_total` is rejected for every actor.
- Delivery consumes on-hand exactly once (30 → still CONFIRMED, 100 →
  DELIVERED/FULFILLED). Lifecycle events (`need.created`,
  `allocation.proposed/confirmed/delivered/cancelled`, `supply.snapshot`)
  are recorded without PII.
- The Phase 0 in-memory ledger, synchronous `Pipeline`, and
  `MemoryIngestionService` are deleted; the golden path now runs on the
  durable harness. Their coverage moved to the suites above.

## Task 11 — rule critic, one bounded revision, medical gate

- Deterministic rules flag over-allocation, >50 km, resource/unit mismatch,
  duplicate candidates, ambiguous location, and implausible quantities.
- The LLM critic is advisory and called outside transactions. Rejection or
  a malformed response produces a review case and no reservation. Exactly
  one revision is permitted and it is deterministic (drop unusable drafts,
  cap quantities); critic free text can never change IDs, resources, or
  quantity ceilings — proven with a malicious `revise` verdict demanding
  1,000,000 l. A revise that changes nothing is refused (no second revise).
- Medical resources/terms (`insulin, oxygen, injury, pregnant, dialysis`)
  bypass the critic and automatic commitment entirely: the need keeps its
  +20 scheduling priority, is flagged `needs_human_review`, creates a
  MEDICAL review case, and the sender is told a coordinator must review —
  never that stock is promised.

## Task 12 — outbox, threaded replies, coordinator handoff, golden path

- Outbox: commit-without-send recovery, retryable timeout under the same
  `outbox:<uuid>` key, duplicate-worker claim delivers once, threaded
  replies preserve the provider message ID, distinct logical notifications
  for one message each deliver exactly once, and a permanent failure writes
  an `outbox.failed` event plus an operator review case.
- After a proposal commits, exactly one `CONFIRM_ALLOCATION` grant
  (SHA-256 hash stored, 30-minute expiry) is created in the same
  transaction and privately emailed only to the assigned verified depot
  coordinator. The requester never receives it; it appears in no public
  projection (asserted against the map snapshot JSON). With no verified
  coordinator, a `ROLE_ASSIGNMENT` review case replaces the grant and
  operators receive a non-token review-queue link.
- Crash proof: killing the worker after registration (critic fault) leaves
  one registered need and no allocation; the retried job rebuilds matching
  from the `matching.completed` event trail and commits exactly one
  allocation, one recommendation, and one confirm request.
- Duplicate needs from a second sender corroborate the existing need
  (quantity unchanged, sender told the existing RN). A later supply
  rematches open needs by priority and notifies the original requester.
- Migration `0002` adds `outbound_emails.mailbox_id` (direct, non-reply
  email needs a sending mailbox) and widens `kind` for per-target
  notification kinds; model and migration are in sync (`alembic check`).

## Deliberate design notes

- **Delayed acknowledgement (15 s) and sender-verification emails** remain
  Task 21 / Task 13 work. Phase 2 proves the underlying guarantee they rely
  on: every logical notification for one inbound message carries its own
  stable outbox key and delivers exactly once (Task 12's outbox suite).
- **Grant execution** (form, session, CSRF, consumption) is Task 14;
  `services/actions.py` owns grant creation so the handoff is transactional.
  The `/actions/{token}` URL is issued but intentionally not yet served.
- **SQLite concurrency:** locking is behavioral on SQLite (single writer);
  the PostgreSQL multi-worker proof remains Task 22, as planned.

## Review response (hostile review of the Phase 2 diff, 2026-11-22)

A fresh reviewer failed all four areas on scenario analysis; every finding
was traced and dispositioned. Final suite after fixes: **288 passed**.

| # | Severity | Disposition |
|---|---|---|
| F1 | High | **Fixed.** `_correction_work` now runs only when the correction actually merged in this transaction (status re-read after `_handle_correction`); late/second/wrong-sender replies can no longer re-register reports, resurrect snapshots, or receive recommendations. Regression: `test_linked_reply_after_completion_stays_in_review`. |
| F2 | High | **Fixed.** `AllocationService.commit` refuses CANCELLED needs and no-ops for human-review needs; the handler skips cancelled needs in both matching phases and recovery only rebuilds OPEN/PARTIAL needs. |
| F3 | High | **Fixed.** A snapshot whose unit differs from actively reserved stock is rejected (`AvailabilityError` → registration failure/review); delivery refuses unit-mismatched allocations. |
| F4 | Medium | **Fixed.** Medical detection uses stems (`injur*`, `pregnan*`, medication/medical/asthma/inhaler/fracture, broken limb) in addition to the plan's five terms and the medicine/first-aid resources. This deliberately errs toward review per Decision 11. |
| F5 | Medium | **Fixed.** Duplicate decisions require equal units; a medical corroborating report creates a MEDICAL review case and a medical notice instead of a plain corroboration reply. |
| F6 | Medium | **Fixed.** Critic contexts are prepared in a short read session that closes before the LLM call (`CriticService.prepare`/`review` split); no transaction is held across provider latency. |
| F7 | Medium | **Fixed.** Lease-expired permanent outbox failures create the same operator review case as provider-exception failures. |
| F8 | Medium | **Fixed.** Recommendations carry one line per allocation; a split never credits one depot with another's quantity (golden split test). |
| F9 | Medium | **Fixed.** The quantity-failure notice no longer claims "nothing was reserved"; it scopes the denial to the failed line (compound test). |
| F10 | Medium | **Fixed.** A second supply line for the same (entity, resource) in one message is a registration failure, never a silent overwrite. (Re-registration resurrection was reachable only via F1, now gated.) |
| F11 | Low | **Fixed.** Draft/revision/commit quantities quantize with ROUND_DOWN; a fractional property case proves drafts never exceed caps. |
| F12 | Low | **Fixed.** Sub-resolution (≤0.000 after truncation) need/delivery quantities are rejected; delivery events record the quantized amount actually applied. |
| F13 | Low | **Fixed.** Every capped or dropped draft writes an `allocation.adjusted` event (approved vs committed quantity + reason). |
| F14 | Low | **Fixed.** The grant guard looks for a *live* (unexpired) grant specifically — an expired grant is reissued exactly once — and the 30-minute TTL starts at issue time, after critique. |
| F15 | Low | **Deferred to Task 22 by plan.** Snapshot-vs-commit lock ordering on PostgreSQL can deadlock in theory; one transaction aborts and the durable job retry recovers. SQLite cannot express it; the concurrency proof owns the final ordering audit. |

Checks that did not break (reviewer-confirmed): the LLM cannot alter
quantities, supply IDs, or obtain a second revision; no token appears in
event payloads, operator email, or the public projection; migration `0002`
matches the model.

## Unresolved gates

| Gate | Why it is unresolved |
|---|---|
| Live email → extraction → critic → reserved proposal → threaded reply → public map | No owner-provided Agentboxd/OpenAI/MojoAuth/Momen credentials. `tests/e2e/test_live_contract.py` is an explicitly skipped gate (`RESILINET_LIVE_CONTRACT=1`); it is not evidence of live success. |
| ≤30 s inbound→reply p50, ≤5 s reply→map | Not measurable without the live accounts above; no latency is claimed from replay. |
| PostgreSQL concurrent-reservation proof | Task 22 by plan; SQLite tests prove behavior, not concurrent locking. |

# Phase 4 Evidence — Public API, Momen Contract, Accessible Map, and Lite View

Status: Tasks 17 and 18 implemented and locally verified on 2026-11-22. Task 19
is implemented only up to the recorded local contract; the Momen editor spike,
publish, and measured reply-to-map latency are **blocked on account access** and
are not claimed. The Phase 4 exit gate (live reply followed by a visible Momen
update within ≤5 s) is therefore **not met**. No Momen, MojoAuth, Agentboxd, or
OpenAI account was used. SQLite proves behaviour, not concurrent locking.

Environment: Python 3.11.16, SQLite 3.53.1, `uv` with the frozen `uv.lock`. No
new dependencies this phase; CORS and static files use FastAPI/Starlette
already in the lockfile. `osv-scanner` is not installed on this machine, so no
fresh dependency scan was run; the lockfile did not change.

## Commands and results

| Command | Result |
|---|---|
| `uv run pytest -q` | **444 passed, 1 skipped** (the pre-existing live-contract gate) |
| `uv run ruff check src scripts tests` | **All checks passed** |
| `uv run mypy src` | **Success: no issues found in 50 source files** |
| `uv run pytest tests/integration/test_public_api.py tests/integration/test_public_views.py tests/e2e/test_momen_contract.py -q` | **30 passed** (14 + 8 + 8) |
| `uv run python scripts/verify_public.py --database-url sqlite+aiosqlite:////tmp/opencode/verify-public.db` | **exit 0**, `ok: true`, `findings: []`, `revalidation_status: 304`, lite transfer **9,148 bytes** |

Baseline before this phase was 413 passed, 1 failed, 1 skipped (415 total).
The suite is now 445 total: 30 new Phase 4 tests plus one repaired
pre-existing test (below). The verification script forces `app_env=test`; it
is a local/test-mode check, not live deployment verification.

Seeded verification payload (temp SQLite DB, `demo-incident`): 2 entities,
1 need, 1 supply, 1 allocation; ETag
`"abe1819c672e6677ae86b964b35bfbd7b9cf1a24dc830ee94ca877defa368cbe"`;
`If-None-Match` revalidation returned 304 with an empty body; `/replay` rendered
10,078 bytes, `/replay/lite` 6,731 bytes plus 2,417 bytes of local CSS.

## Task 17 — public read API

- `src/resilinet/services/public.py`: explicit allow-list dict projections (no
  ORM serialization), `PUBLIC_ROW_LIMIT = 500`, SQL filter predicates.
  Filter semantics: `resource` narrows needs, supplies, and allocations;
  `urgency`/`status` are need attributes and narrow needs plus (through their
  needs) allocations. Unknown filter values are 400s; unknown incidents are
  404s; projection invariant violations are generic 500s with no internals
  leaked.
- Row lists are capped at 500 per collection, detected with a limit+1 probe
  and reported in a `truncated` field — never silently. Summary counts and
  totals are exact aggregates computed independently of the cap (SQL counts;
  Decimal totals summed in Python over narrow column projections, never via
  a float aggregate), so they stay correct for large incidents.
- Derived availability is `on_hand` minus reservations in
  `ACTIVE_ALLOCATION_STATUSES`; a negative result raises
  `PublicProjectionError` rather than clamping to zero. Only the map exposes
  availability, so only the map can fail closed on it; `/needs` and
  `/summary` are unaffected (regression-tested). Quantities serialize
  as three-decimal strings; approximate distance rounds to 0.1 km.
- Cache validation: a quoted SHA-256 revision validator over the
  representation kind, row counts, newest `updated_at` per collection, the
  audit-event count/tail, and the canonical filter set. It assumes mutations
  go through the service layer (which bumps timestamps and appends events);
  direct database repair outside that invariant may not invalidate.
  `Cache-Control: no-cache`; `If-None-Match` honours exact, weak (`W/`), and
  `*` forms and returns an empty 304. Summary totals use Decimal arithmetic:
  cancelled needs contribute no requested/unfulfilled quantity, but
  quantities already delivered against them remain in delivered totals.
- CORS is installed only when `RESILINET_MOMEN_ORIGINS` is set: exact
  configured origins only (`"*"` is rejected at startup), `GET` only, no
  credentials. `If-None-Match` is a preflight-allowed header and `ETag` is
  exposed, so a browser client can actually use conditional requests. A
  `POST` preflight from an allowed origin is rejected (400) and never grants
  the method.

## Task 18 — replay mirror and lite view

- `src/resilinet/web/replay.py` serves `/replay` and `/replay/lite` in
  test/replay modes only; both return 404 in live mode. Unknown incidents and
  invalid filters render generic HTML error pages (404/400), never tracebacks.
- Markers are one per entity with shape + text label + colour (circle shelter,
  rect depot, diamond hub, triangle other), an `aria-label`, and an SVG
  `<title>`; coordinates are a linear schematic projection, explicitly not
  road distance. Entities without coordinates stay in the tables and never
  get a marker. The page carries header/nav/main landmarks, labelled GET
  filter forms that work without JavaScript, a `<details>` table toggle,
  quantity-basis (`stated`/`estimated`), unverified, and human-review text,
  and an `aria-live="polite"` status line.
- `static/replay.js` polls the same-origin map JSON every 1 s, seeded with
  the server-rendered ETag so the first poll can already return 304; an
  in-flight guard prevents overlapping renders. It re-renders via
  `textContent` only (no HTML injection from data), announces changes
  without moving focus, and restores a focused marker (matched by its
  accessible name) after re-render. `static/app.css` is local-only and includes
  `prefers-reduced-motion` and `prefers-contrast` rules; no external URL
  appears in either template or the stylesheet (test-enforced).
- `public_headers()` (new in `src/resilinet/security.py`): scripts are
  same-origin only and never inline; styles additionally allow the inline
  `<style>` block inherited from the base template. Private pages keep the
  stricter `no-store` CSP.
- The lite view has no script and no SVG and renders the same rows. Its
  sub-100 KB property is verified for the seeded dataset (9,148 bytes
  transferred including local CSS); a large incident renders proportionally
  more rows, so the budget is not claimed per-incident.

## Task 19 — Momen contract (local half only)

- `tests/fixtures/momen/map_snapshot.json` is generated by the real
  `map_snapshot` projection; `tests/e2e/test_momen_contract.py` asserts the
  live projection still equals the fixture, that every contract field set
  equals the fixture exactly, that the three endpoints exist as `GET` routes
  with exactly the declared query parameters, that the non-token operator
  link exists, and that no action URL, token, session, sender, email, or
  provider payload is bound.
- `frontend/momen/api-contract.json` records `refresh.verified: false` and
  `gate_status: unresolved_account_gate`; a 5,000 ms fallback is explicitly
  not a verified pass of the ≤5 s target. `frontend/momen/README.md` and
  `docs/momen-setup.md` document the blocker and the editor/publish
  checklists.

## Independent review findings and dispositions

A read-only hostile review of the full Phase 4 diff was performed after the
first green run (the dedicated review agent was unavailable due to a harness
error; a general agent reviewed under identical read-only instructions). One
high and seven medium findings were raised; all were reproduced or verified
against the code before acting.

| # | Severity | Finding | Disposition |
|---|---|---|---|
| H1 | High | Row lists silently truncated at 500; summary counts/totals computed over the truncated subset, so totals were wrong (not just incomplete) for large incidents, and map allocations could reference needs outside the loaded window. | **Fixed.** Limit+1 probe with a `truncated` field on map/needs bodies; allocations are now always constrained to the loaded need set; summary counts/totals are exact uncapped aggregates. Regression: `test_summary_totals_stay_exact_when_row_lists_truncate`. |
| M1 | Medium | Delivered quantities against a later-cancelled need disappeared from summary delivered totals, contradicting the totals docstring. | **Fixed.** Delivered history counts for cancelled needs; requested/unfulfilled still exclude them. Regression: `test_summary_keeps_delivered_history_of_cancelled_needs`. |
| M2 | Medium | One negative-availability supply made `/needs` and `/summary` return 500 although neither exposes availability. | **Fixed.** Availability is computed (and can fail closed) only for the map. Regression: `test_negative_availability_poisons_only_the_map`. |
| M3 | Medium | Browser CORS could not use the ETag flow (`If-None-Match` not preflight-allowed, `ETag` not exposed), and `RESILINET_MOMEN_ORIGINS=["*"]` silently produced a wildcard allow-origin. | **Fixed.** `allow_headers=["If-None-Match"]`, `expose_headers=["ETag"]`, and a settings validator rejecting `"*"`. Regressions: `test_cors_preflight_allows_the_etag_header`, `test_wildcard_momen_origin_is_rejected`. |
| M4 | Medium | All three representations shared one ETag value, and comments overstated the validator's honesty under direct database repair. | **Fixed.** The representation kind is part of the validator material (`test_etags_differ_between_representations`); docs/comments now state the service-layer invariant explicitly. |
| M5 | Medium | Momen contract tests over-claimed: "byte-identical" fixture, endpoint GET asserted from the JSON string rather than the route, CORS asserted by middleware presence only, one tautological parity assertion. | **Fixed.** Route `methods` and full CORS options are asserted; the tautology was replaced with a list-parity assertion; README wording corrected to parsed-JSON equality with the HTTP layer explicitly covered elsewhere. The refresh/lite assertions remain as deliberate tripwires against silently flipping `verified` to true. |
| M6 | Medium | Derived availability scans active allocation rows and the revision counts the incident event log on every request, so a 304 saves no server work. | **Accepted for P0, partially mitigated.** Availability is now map-only (M2), the scan reads three narrow columns, and the event count is incident-indexed. A monotonically increasing incident revision column is the proper fix and belongs with the Phase 5 observability work. |
| M7 | Medium | Urgency/status filters do not narrow supplies or entities, which the generic filter presentation did not explain. | **Documented, behaviour intentional.** Supplies have no urgency/status; semantics are now stated in the service docstring, `api-contract.json` (`filter_semantics`), and this file. |
| M8 | Medium | The lite view's "under 100 KB" wording was unconditional while the service permits 500 rows per collection. | **Fixed (wording).** The claim is now explicitly a seeded-data property in the route docstring and this file. |
| L1–L3 | Low | Schematic SVG inlined via `\|safe` without sanitisation and re-read per request; CSP docstring overstated; replay JS re-downloaded fully on first poll, could overlap renders, and restored focus by shared `href`. | **Fixed.** Active SVG constructs are stripped and the asset is cached; the CSP docstring is accurate; the JS seeds the server ETag, guards in-flight polls, and restores focus by accessible name. JS behaviour remains code-inspected, not browser-tested (Task 24). |
| L4 | Low | `Entity.name` is published verbatim; a mis-extracted personal name would be public. | **Accepted residual risk.** Facility names are intended public; extraction-quality controls belong to the Phase 1/5 extraction work, and no sender-derived field is projected. Recorded below. |
| L5 | Low | `scripts/verify_public.py` forces test mode, so its result is not live verification. | **Documented** in the script output section above; no live claim is made from it. |

## Pre-existing test defect repaired (not a production change)

`tests/e2e/test_participant_journeys.py::test_rematch_job_cannot_touch_another_incidents_need`
inserted a `Job` without `available_at`. The column default is real `utcnow()`
while the harness clock is frozen at `2026-11-22 09:00 UTC`, so the worker's
due-claim (`available_at <= now`) correctly skipped the job — the test passed
only earlier in the day and failed once real time passed 09:00. Every other
direct-Job test already sets `available_at=NOW`; this one now does too, with
an explanatory comment. No production code changed, and the cross-incident
assertion (no allocation, need stays `OPEN`) is unweakened.

## Unresolved gates

| Gate | Why it is unresolved |
|---|---|
| Momen editor: native map acceptance, scheduled refresh interval, publish URL/policy | No owner editor session for the target workspace. Nothing about the hosted page is claimed. |
| Phase 4 exit gate: reply-to-map ≤5 s (p50/p95) | No published Momen page exists, so latency is unmeasured. A 5 s poll would not pass this gate anyway. |
| Hosted Momen `/lite` under 100 KB with JS disabled | Same account gate. The 9,148-byte figure is the local FastAPI lite view, not the hosted page. |
| Browser keyboard-only / 200% zoom / narrow-viewport pass on `/replay` | Tests cover landmarks, labels, focus restore, and no-JS forms; no browser pass was performed. Task 24 by plan. |
| Live MojoAuth/Agentboxd/OpenAI verification | Carried from Phases 2–3: no owner credentials; `tests/e2e/test_live_contract.py` remains an explicitly skipped gate. |
| PostgreSQL concurrency proof | Task 22 by plan; SQLite proves behaviour and idempotency, not row-lock serialization. |
| Public entity-name content risk | `Entity.name` is published verbatim by design (facility names are public). A mis-extracted personal name would be exposed unauthenticated; mitigation belongs to extraction quality controls, not the projection, which never emits sender-derived fields. |
| Per-request revision cost on very large incidents | Accepted for P0 (see M6 above): narrow-column scans plus an incident-indexed event count. A stored incident revision counter is deferred to Phase 5 observability work. |

# Phase 3 Evidence — Identity, Secure Actions, and Minimal Review

Status: Tasks 13–16 implemented and locally verified on 2026-11-22. No live
MojoAuth, Agentboxd, OpenAI, or Momen account was used. The MojoAuth adapter is
exercised against recorded HTTP shapes with `httpx.MockTransport` only; no live
identity result is claimed. SQLite proves behaviour, not concurrent locking.

Environment: Python 3.11.16, SQLite 3.53.1, `uv` with the frozen `uv.lock`
(51 packages). New direct dependencies this phase: `jinja2` 3.1.6 and
`python-multipart` 0.0.34 (server-rendered private pages and form parsing).

## Commands and results

| Command | Result |
|---|---|
| `uv run pytest -q` | **414 passed, 1 skipped** (the pre-existing live-contract gate) |
| `uv run ruff check src scripts tests` | **All checks passed** |
| `uv run mypy src` | **Success: no issues found in 49 source files** |
| `alembic upgrade head` on a scratch SQLite DB | applied `0001 → 0002 → 0003 → 0004` |
| `alembic check` | **No new upgrade operations detected** |
| `alembic downgrade base` | clean `0004 → 0003 → 0002 → 0001 → base` |
| `osv-scanner scan source -r .` (v2.7.1) | scanned `uv.lock`, 51 packages, **No issues found** |
| `uv run python scripts/seed_memberships.py …` on a scratch SQLite DB | seeded 1 operator and 1 depot coordinator; both senders remain unverified and no token was issued |

Phase 3-focused suites (81 tests):

```text
tests/unit/test_mojoauth.py                      9 passed
tests/unit/test_notification_text.py             3 passed
tests/unit/test_security.py                      8 passed
tests/integration/test_identity.py              17 passed
tests/integration/test_action_security.py       21 passed
tests/e2e/test_participant_journeys.py           8 passed
tests/integration/test_operator_reviews.py      12 passed
tests/integration/test_memberships.py            3 passed
```

Review regressions were also added to the extended Phase 2 suites:
`tests/integration/test_grant_handoff.py` (3), `tests/integration/test_outbox.py`
(9), and `tests/integration/test_reservations.py` (22).

## Task 13 — MojoAuth verification and server-side sessions

- **Outstanding request per sender:** migration `0003` adds a partial unique
  index on `verification_requests(sender_id)` where `status = 'pending'`.
  Repeated worker deliveries reuse the same request; a provider failure marks
  it `FAILED` so a later attempt can retry. An expired request is marked
  `EXPIRED` before a new one is created.
- **One send per request:** migration `0004` adds `send_claimed_at` and the
  sender atomically claims a request before the provider HTTP call, so two
  concurrent triggers cannot send two magic links or overwrite each other's
  provider state. A crashed claim becomes retryable after two minutes.
- **Least data:** the provider state id, sender binding, redirect intent, and
  status are persisted; the provider's magic-link email is the only thing that
  reaches the user. ResiliNet does not fabricate a login URL.
- **POST completes, GET previews:** the provider callback GET renders a
  confirmation page and consumes nothing; only the POST marks the request
  complete, sets the cookie, and redirects. The POST must also present a nonce
  set as an HttpOnly `SameSite=Lax` cookie by the confirmation GET, so a blind
  cross-site POST cannot create a session. Mailbox scanners and link
  previewers that follow the URL cannot burn the link or receive the session.
- **Binding:** the callback looks up the pending request by the provider's
  `state_id`, checks expiry, calls `GET /users/status`, and refuses a missing,
  unauthenticated, or different normalized email. A completed request cannot be
  replayed. A transient provider error leaves the request usable for another
  click; a binding mismatch marks it `FAILED`.
- **Sessions:** 32-byte URL-safe cookies; only SHA-256 digests are stored. The
  CSRF token is derived from that digest with a domain separator and its hash
  is stored in `sessions.csrf_hash`; a cross-site request that cannot read the
  HttpOnly cookie cannot produce it. Cookies are `Path=/; HttpOnly;
  SameSite=Lax; Max-Age=…`, with `Secure` added in live mode. Logout revokes the
  row and requires the session's own CSRF token.
- **Verification is not authority:** the callback sets `senders.verified` and
  upgrades that sender's prior needs to `verified=True`, but creates no
  `participant_roles` row. An ordinary verified sender gets a session and a 403
  on operator pages.
- **Throttled operator start:** `POST /operator/login` always returns the same
  generic response. It sends only for an **existing** sender (it never creates
  one), applies a per-address limit and a global operator-intent limit before
  calling MojoAuth, and therefore cannot be used to mail arbitrary addresses or
  grow the sender table.
- **Processing intact:** the worker requests verification in a `finally` after
  the message's work. A provider outage logs and marks the request failed; it
  never fails or retries the mail job.
- **Adapter honesty:** `MojoAuthAuthProvider` implements the documented
  `POST /users/magiclink` and `GET /users/status?state_id=` contract, never
  echoes provider error bodies, and maps timeouts/429/5xx to retryable
  `ProviderUnavailableError`. The live account spike remains blocked.

## Task 14 — scoped single-use action grants and authorization

- **Grants:** 32-byte URL-safe tokens, SHA-256 only in `action_grants`, 30-minute
  default TTL, bound to incident, sender, kind, entity/need/allocation target,
  and (for email actions) the immutable pending payload hash.
- **GET never mutates:** `/actions/{token}` renders a safe form only. Unknown,
  expired, and consumed tokens all return the same 404 page. A missing session
  returns 401 without revealing the target. A forwarded link or another
  verified sender sees a generic 403.
- **POST-only execution:** the route requires the session's CSRF token before
  opening the transaction, re-loads the grant `FOR UPDATE`, and re-checks the
  sender/incident, role or ownership, target binding, and payload hash inside
  the same transaction as the mutation. The grant is then claimed with a
  conditional `UPDATE … WHERE consumed_at IS NULL AND expires_at > now …
  RETURNING` before any mutation; a failed mutation rolls the claim back with
  the transaction. A successful one makes the second POST a 404.
- **Confirmation:** only a `DEPOT_COORDINATOR` for the allocation's supply
  entity or an incident `OPERATOR` may confirm. Confirmation additionally
  requires the original requester to be verified **or** a resolved
  `APPROVE_PROPOSAL` review case for that need, and is blocked while a MEDICAL
  review case is open. Verification alone never grants depot authority.
  Regression: `test_open_medical_review_blocks_confirmation_until_resolved`.
- **Delivery/cancellation:** only the original requester, the need's assigned
  `ENTITY_COORDINATOR`, or an incident `OPERATOR` may report a delivery, cancel
  an allocation, or cancel a need. Requester delivery reports consume stock
  only for their own allocation; they cannot write a snapshot.
- **Stock:** only a depot coordinator for that entity or an operator may apply
  an on-hand snapshot. A unit conflict raises a state error rather than
  silently replacing reserved stock.
- **Audit:** every success writes `action.consumed` plus the domain event
  (`allocation.confirmed`, `allocation.delivered`, `allocation.cancelled`,
  `need.cancelled`, `supply.snapshot`) and a private result email. No token is
  placed in an event payload, outbox body, or log.

## Task 15 — participant journeys

- **No direct email mutation:** parsed `SUPPLY`, `STATUS_UPDATE`, and `CANCEL`
  messages persist immutable `PendingAction` rows (canonical payload + SHA-256
  hash) and never touch `supplies`, `needs`, or `allocations` during extraction.
- **Private link only when authorized:** a depot coordinator (or operator)
  receives one `action_request:<pending-id>` email with `/actions/{token}`.
  An unauthorized supply sender produces a `ROLE_ASSIGNMENT` review case and a
  non-token reply; an unauthorized cancellation/delivery produces an `OTHER`
  review case and a non-token reply. The pending record remains for audit.
- **Residual rematch:** an applied stock snapshot computes
  `ranked_open_needs` for that resource under the action transaction and queues
  one durable `PROCESS_MESSAGE` job carrying an explicit `rematch_need_ids`
  list. The worker matches only those residual needs; it is never a global
  rematch. New allocations notify their requesters; a snapshot that invalidates
  reservations notifies each affected requester.
- **Measured journey:** a 300-gallon supply email produced no stock row, one
  pending action, one live grant, and one private link. A verified session plus
  CSRF POST applied exactly `1135.500 l`; the residual 120 l need was then
  matched by the durable job.
- **Delivery reports:** a `STATUS_UPDATE` naming an `AL-` reference with one
  stated quantity becomes a `DELIVER_ALLOCATION` pending action. The requester
  delivered 30 l and then 90 l against a 120 l allocation; on-hand stock moved
  from 500 to 380 and the allocation ended `DELIVERED`.
- **Cancellation:** `CANCEL RN-…` releases every active sibling allocation;
  `CANCEL AL-…` releases that allocation. Delivered history is untouched.
- **Status query:** `QUERY STATUS RN-…` is read-only and answers only the
  requester, the entity coordinator, or an operator. An unrelated sender gets
  the generic wrong-sender response with no status details. The query writes no
  domain change.

## Task 16 — minimal operator review

- **Queue/detail:** `/operator/reviews` and `/operator/reviews/{id}` require a
  verified session plus an incident-wide `OPERATOR` role. Cases are filtered by
  the session's incident; another incident's case is a 404.
- **Escaped output:** case reason, stored payload, and the source message
  excerpt are rendered through Jinja autoescaping; a `<script>` excerpt appears
  as `&lt;script&gt;` and never as markup.
- **Resolutions:** supported combinations are enforced per case kind:
  `MEDICAL`/`QUANTITY` accept approve or revise; `DUPLICATE` accepts
  mark-duplicate or approve; `CRITIC` accepts approve or reject;
  `ROLE_ASSIGNMENT` accepts assign or reject; `OTHER` accepts approve, reject,
  or assign. An unsupported pair is a 400 and leaves the case open.
  `APPROVE_PROPOSAL` clears the review block and schedules the ordinary
  matching job (no stock promise); `REVISE_QUANTITY` calls
  `AllocationService.update_need`, so a value below `delivered_total` is
  rejected and never clamped; `REJECT` records the refusal and rejects a linked
  proposed allocation; `MARK_DUPLICATE` preserves both needs and all
  corroboration without merging quantities; `ASSIGN_DEPOT_COORDINATOR` creates
  the incident-scoped role and reissues the attached pending action's private
  link after validating that the pending action belongs to that sender, entity,
  incident, and `UPDATE_SUPPLY` kind.
- **Authorization and audit:** every resolution re-checks the operator role in
  the same transaction, requires the session CSRF token, locks the case, claims
  it with a conditional `UPDATE review_cases … WHERE status = 'open' …
  RETURNING`, and writes `review.resolved` with case kind, resolution,
  need/allocation refs, and actor. A missing CSRF token or a failed resolution
  leaves the case open.
- **Medical initial state:** a medical need starts `needs_human_review=true`
  with no allocation and no stock promise; approving it does not promise stock,
  it only permits normal reservation to run.
- **Membership bootstrap:** `scripts/seed_memberships.py` creates the sender
  row and incident-scoped role for an operator, depot coordinator, or entity
  coordinator. It never marks the sender verified and issues no token; the
  sender must still complete MojoAuth. Verified on a scratch SQLite database:
  one operator (entity null) and one depot coordinator (entity set).

## Deliberate design notes

- **`STATUS_UPDATE` mapping:** `STATUS_UPDATE` with an `AL-` reference and one
  stated quantity is a delivery report. With an `RN-` reference it is routed to
  operator review because the approved action kinds do not include a
  participant need-revision; operators use `REVISE_QUANTITY`. A status update
  without a reference is treated as a notice, never as a stock snapshot.
- **Approval vs verification:** operator approval is a resolved review case,
  not a mutation of `needs.verified`. `needs.verified` keeps meaning "the
  sender verified their address". The confirmation gate checks both
  independently.
- **Email action retry:** if a grant exists but its private email did not
  commit (crash between writes), the old grant is invalidated and a fresh
  token/email is issued. A live grant with its email already queued is never
  duplicated.
- **Golden path harness:** the Phase 2 fake golden path now performs the same
  authorized supply action as setup, so it still proves matching without
  reintroducing an email mutation path.
- **SQLite concurrency:** `FOR UPDATE` is a no-op on SQLite. The action and
  review transactions are written with row locks for PostgreSQL; the
  multi-worker concurrency proof remains Task 22.

## Review response

A fresh `review` subagent audited the cumulative Phase 3 diff (identity,
action execution, participant journeys, operator review) for forwarded/cross-
incident links, CSRF/replay, payload tampering, trust-gate bypass, rematch
abuse, PII/token leakage, transaction boundaries, and migration drift. It
reached its step limit before covering notifications, templates, handlers,
allocations, config, and the tests, but it produced eight actionable findings.
A second targeted review covers the remaining areas.

| # | Severity | Finding | Disposition |
|---|---|---|---|
| 1 | High | `/auth/callback` was a state-mutating GET; mailbox prefetchers could consume the link and receive the session. | **Fixed.** GET now renders a confirmation page and consumes nothing; POST completes verification, sets the cookie, and redirects. Regression: `test_callback_get_previews_without_consuming_or_creating_a_session`. |
| 2 | High | Unauthenticated `/operator/login` could mail arbitrary addresses, create senders, and had no global bound. | **Fixed.** Login now sends only for an existing sender, never creates one, and applies a per-address plus global operator-intent limit before calling MojoAuth. Regressions: `test_unknown_address_never_receives_a_magic_link_or_creates_a_sender`, `test_global_operator_login_throttle_bounds_rotated_addresses`. |
| 3 | Medium | Concurrent verification triggers could send duplicate magic links and overwrite the provider state id. | **Fixed.** Migration `0004` adds `send_claimed_at`; one `UPDATE … RETURNING` claim precedes the provider call, with a two-minute stale retry. Regression: `test_one_send_claim_prevents_a_duplicate_magic_link`. |
| 4 | Medium | `ASSIGN_DEPOT_COORDINATOR` reissued a pending-action link without checking it belonged to the granted sender/entity. | **Fixed.** The resolution locks the pending action and requires matching incident, sender, entity, kind (`UPDATE_SUPPLY`), and pending status. Regression: `test_assignment_rejects_a_pending_action_for_another_sender`. |
| 5 | Medium | `REJECT` on a need-level case resolved the case without any domain change. | **Fixed.** A resolution/case-kind matrix rejects meaningless combinations; need-level medical/quantity cases must be approved or revised; a rejected role-assignment request also rejects its linked pending action. Regressions: `test_reject_on_a_need_level_medical_case_is_invalid`, `test_duplicate_resolution_is_not_valid_for_a_medical_case`. |
| 6 | Medium | Resolution dispatch ignored case kind, so an unrelated resolution could clear a block. | **Fixed.** `_ALLOWED_RESOLUTIONS` enforces per-kind choices with `ReviewInvalid` for unsupported pairs. Same regressions as #5. |
| 7 | Low | Cross-incident target checks were incomplete for grants. | **Fixed.** `_trio`, `_need`, `_pending`, and `_update_supply` require every referenced row to belong to the grant's incident. Regression: `test_grant_cannot_target_another_incidents_allocation`. |
| 8 | Low | `UPDATE_SUPPLY` could apply a payload entity different from the pending target when `grant.entity_id` was null. | **Fixed.** `authorize` and `_update_supply` require `pending.entity_id`, require payload equality, and require the entity's incident to match. Regression: `test_stock_payload_entity_must_match_the_pending_target`. |

A second targeted review covered the remaining files and produced additional
edge-case findings. It did not reach every requested path (templates, the
ActionService remainder, and some config paths), so those remain supported by
the Phase 3 suites and the first pass's sound list rather than an independent
read.

| # | Severity | Finding | Disposition |
|---|---|---|---|
| 1 | High | A forged/malformed rematch job could carry another incident's need IDs. | **Fixed.** `_process_rematch` filters IDs by the source message's incident, and `_run_matching` skips needs outside its incident. Regression: `test_rematch_job_cannot_touch_another_incidents_need`. |
| 2 | High | `_grant_or_review` used a pre-critic `now`, and a coordinator change left the old link live. | **Fixed.** TTL and the live-grant check use the real clock inside the write transaction; live grants held by a different sender are invalidated; an existing grant without its private email is reissued. Regressions: `test_grant_expiring_during_critic_latency_is_reissued`, `test_coordinator_change_invalidates_the_old_grant_and_issues_a_new_one`. |
| 3 | Medium | Medical corroboration said "No stock has been reserved" even for an existing reserved need. | **Fixed.** A corroboration-specific text states that existing reservations are unchanged and under review. Test: `test_medical_corroboration_does_not_claim_an_empty_reservation` plus the golden-path assertion. |
| 4 | Medium | A missing `RN-` query promised a review that was never created. | **Fixed.** The reply now states no reference exists, nothing changed, and no review was opened. Regression: `test_status_query_for_an_unknown_reference_is_truthful`. |
| 5 | Medium | `AL-` queries were advertised but unhandled. | **Fixed.** `QUERY STATUS AL-…` resolves the allocation, applies the same authorization, and returns allocation status. Regression added to `test_query_status_is_read_only_and_scoped_to_authorized_sender`. |
| 6 | Medium | Recovery completion used a bounded 500-event scan. | **Fixed.** Completion is queried by `(event_type, aggregate_type, aggregate_id)` with no limit. |
| 7 | Medium | A late outbox worker could overwrite a reclaimed lease. | **Fixed.** `_record_sent`/`_record_failure` require the claimed attempt count and `SENDING` state under a row lock. Regression: `test_late_worker_cannot_overwrite_a_reclaimed_lease`. |
| 8 | Medium | A supply unit change could corrupt delivered history when nothing was reserved. | **Fixed.** A unit change is rejected when any delivered quantity exists in the old unit. Regression: `test_snapshot_unit_change_after_delivery_is_rejected`. |
| 9 | Medium | Snapshot cancellation cancels whole allocations and prefers proposals over strict newest-first. | **No change.** This is the deliberate Phase 2 contract: proposals first, then newest confirmed, whole allocations released; delivered history is never altered. The plan/evidence wording already describes it. |
| 10 | Medium | A cancelled target still rendered a live action form. | **Fixed.** Authorization now requires a confirmable proposal, a confirmed allocation for delivery, a cancellable allocation/need, or a live stock target; otherwise the link is a generic 404. Regression: `test_confirm_grant_for_a_cancelled_allocation_is_generic_404`. |
| 11 | Medium | A permanently failed notice blocked its replacement forever. | **Fixed.** Guarded enqueue and intake dedupe ignore `FAILED` rows. |
| 12 | Medium | An inbound subject could carry CR/LF into the outbound subject. | **Fixed.** `reply_subject` collapses all whitespace. Test: `test_reply_subject_collapses_header_control_characters`. |
| 13 | Low | `update_need(0.0004)` quantized to zero and left an unmatchable OPEN need. | **Fixed.** Quantize first and reject values below 0.001. Regression: `test_need_revision_that_quantizes_to_zero_is_rejected`. |
| 14 | Low | The status reply counted rejected allocations in `delivered`. | **Fixed.** Rejected allocations are excluded, matching `delivered_total`. |
| 15 | Low | A negative supply snapshot was persisted. | **Fixed.** Rejected before write. Regression: `test_negative_supply_snapshot_is_rejected`. |
| 16 | Low | Operator predicate mismatch between actions and status queries. | **No change.** Both paths require an incident-wide operator row (`entity_id IS NULL`); verified aligned. |
| 17 | Low | A fulfilled need could receive an "open for matching" recommendation. | **Fixed.** Matching skips `FULFILLED` needs in planning and in the write pass. |
| 18 | Low | A missing supply-entity lookup produced a false "no stock reserved" line. | **Fixed.** The recommendation branches on the allocation ref and falls back to "The supplying depot". Test: `test_recommendation_body_does_not_claim_no_stock_when_depot_name_is_missing`. |

A third narrow review covered the callback, redirect helper, action preview,
review-case claim, and private headers. Its findings and dispositions:

| # | Severity | Finding | Disposition |
|---|---|---|---|
| 1 | High | `POST /auth/callback` had no CSRF/browser proof, so a blind cross-site POST could substitute a session. | **Mitigated.** The confirmation GET sets an HttpOnly `SameSite=Lax` nonce cookie and renders the same nonce; the POST requires both. A blind auto-POST now fails. A user who deliberately opens a forwarded magic link can still authenticate as its owner; browser-binding would break cross-device verification and is recorded as a residual risk. Regression: `test_callback_get_previews_without_consuming_or_creating_a_session`. |
| 2 | Medium | `local_redirect_path` allowed `/\evil.com` and control characters, which browsers can normalize into a cross-origin redirect. | **Fixed.** Backslashes, percent-decoded `//`, and control characters are rejected. Tests: `tests/unit/test_security.py`. |
| 3 | Medium | A malformed DELIVER payload could raise a 500 on the action preview instead of a generic 404. | **Fixed.** Delivery preview parsing raises `ActionInvalid`. Regression: `test_malformed_delivery_payload_preview_is_a_generic_404`. |
| 4 | Medium | A provider send whose request row was no longer PENDING could leave an emailed link unbound. | **Accepted as a narrow race.** The conditional send claim prevents normal concurrency; an unexpected non-PENDING row now logs `identity_magic_link_unbound`. The link is already emailed and cannot be safely rebound after the request expired or completed. |
| 5 | Low/Medium | Private headers used `setdefault`, so a weaker route header could survive; unhandled 500s bypass the user middleware. | **Fixed for handled responses.** Private headers are now assigned unconditionally on `/actions/`, `/operator/`, and `/auth/`. Unhandled 500 bodies contain no token and are generated outside user middleware; this is recorded rather than adding a broad exception handler. |

## Unresolved gates

| Gate | Why it is unresolved |
|---|---|
| Live MojoAuth magic-link callback and identity verification | No owner-provided MojoAuth account/API key. The adapter contract is documented and MockTransport-tested only. |
| Live email → verification → action link → confirmed/delivered state | No live Agentboxd/MojoAuth credentials. Fake inbox and identity prove contracts, not live delivery. |
| PostgreSQL concurrent double-submit and reservation proof | Task 22 by plan; SQLite tests prove behaviour and idempotency, not row-lock serialization. |
| Accessibility audit of `/actions` and `/operator/reviews` | Task 24 by plan. The pages use semantic headings, labels, fieldsets, focus styles, and no-JS forms; no Axe/Lighthouse score is claimed yet. |
| Browser-bound magic-link login CSRF | The confirmation GET/POST nonce blocks blind cross-site POSTs, but a user who deliberately opens a forwarded magic link can still authenticate as its owner. Browser binding would break cross-device verification and is deferred. |

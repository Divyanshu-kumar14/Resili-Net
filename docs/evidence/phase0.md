# Phase 0 Evidence — 2026-11-22

Scope: implementation plan Tasks 1–3. This file records only results produced
in this workspace. No live sponsor call has been made; the account gates are
listed below and remain blocked.

## Environment

- Linux x86_64, Python 3.11.16 (uv-managed)
- uv 0.12.17, pytest 9.2.0, pytest-asyncio 1.5.0
- ruff 0.17.6, mypy 1.20.2
- FastAPI 0.165.0, Pydantic 2.14.4, SQLAlchemy 2.1.9
- osv-scanner 2.7.1 (`/tmp/opencode/osv-scanner`)

## Commands and results

| Command | Result |
|---|---|
| `uv lock --check` | `Resolved 40 packages`, exit 0 |
| `uv sync --frozen` | clean install from the lockfile |
| `uv run pytest -q` | **84 passed** |
| `uv run ruff check src tests scripts` | `All checks passed!` |
| `uv run mypy src` | `Success: no issues found in 23 source files` |
| `osv-scanner scan source -r .` | `No issues found` over 40 locked packages |
| `uv run uvicorn resilinet.main:app --port 8123` + `GET /api/v1/health` | `200 {"status":"ok"}`, `X-Trace-Id` present |
| `RESILINET_APP_ENV=live uv run uvicorn resilinet.main:app` | exit 1: `live mode requires these settings: agentboxd_api_key, agentboxd_webhook_secret, mojoauth_api_key, openai_api_key` |
| Uvicorn access log with `GET /actions/super-secret-token?token=also-secret` (404: no action route exists in Phase 0) | both the structured log and the access log show `/actions/{token}`; the token and query string appear zero times |

Dependency remediation: the first scan found `PYSEC-2026-1845` in pytest 8.4.2
(fixed in 9.0.3). The dev dependency was raised to `pytest>=9.0.3` and
`pytest-asyncio>=1.3`; the re-scan is clean. The vulnerability was not
suppressed or ignored.

## Golden-path proof (Fake adapters, no network)

Input: “Shelter 4 has 40 people and zero potable water. Near Gandhi Park.” plus
a seeded “Depot B has 300 gallons of potable water on River Road.”

Measured in the test and captured to gitignored `artifacts/phase0/`:

- Need: `RN-000001`, `potable_water`, `120.000 l`, `quantity_source=estimated`,
  `status=matched`.
- Supply: `1135.500 l` on hand (300 gal × 3.785), `1015.500 l` available after
  the proposal.
- Allocation: `AL-000001`, `120.000 l`, `status=proposed`, distance `1.914 km`.
- Reply: contains “recommended”, `~120 l`, “estimated”, the `RN-` reference,
  and “This is a proposal, not a shipment or a dispatch.” It contains neither
  “dispatched” nor “delivered”.
- Map JSON: both entities and the allocation, with no sender address or
  provider message ID anywhere in the payload.

Additional Phase 0 tests cover: duplicate received/enriched/released events
(one need, one reply), retry with the same outbox key (no duplicate email),
a failed send retried without duplicate domain state, snapshot below active
reservations rejected without a success reply, compound-report validation
before mutation, partial-match replies that state the allocated and requested
quantities separately, unlocatable-need clarification retaining `kind=NEED`,
zero-stated NEED estimated from headcount, implausible/sub-quantum quantities
clarified rather than crashing, and HMAC tamper/stale/non-ASCII/missing-header
rejection. Empty-body handling is tested for both full-message and
envelope-shaped payloads.

## Review findings resolved

Source: independent read-only Phase 0 review (CHANGES_REQUIRED).

| Finding | Resolution |
|---|---|
| CRITICAL: slugged `client_id` could merge incidents | SHA-256-derived `client_id_for_incident`; fake and live parity tests with `incident/a` vs `incident a` |
| HIGH: `propose()` double-reserved | Residual computed from delivered + active reservations; second call returns existing active allocations; test added |
| HIGH: random outbox key per attempt | Deterministic UUIDv5 key from incident, provider message ID, and purpose; retry test added |
| HIGH: invalid snapshot persisted then replied success | `check_snapshot` validates before any write/reply; rejected snapshot leaves stock and availability unchanged; test added |
| HIGH: stated-zero NEED crashed | Zero is treated as missing; headcount estimate or clarification; tests added |
| HIGH: action tokens logged | `redact_path` for structured logs plus a `uvicorn.access` filter that drops query strings and redacts `/actions/`; tests added |
| HIGH: live mode accepted SQLite | Live config rejects SQLite; test added |
| HIGH: non-ASCII signature raised `TypeError` | Constant-time byte comparison; test added |
| HIGH: thin live error mapping | String-shaped error codes, 429, empty/malformed claim items, and `gone` ack covered |
| MEDIUM: entity relocated/retyped by a later email | Existing coordinates and type are never overwritten; test added |
| MEDIUM: clarification after partial mutation | Every report is validated before entity/need/supply mutation; compound test added |
| MEDIUM: clarification labelled `OTHER` | `ClarificationRequired.kind` preserves NEED/SUPPLY; test added |
| MEDIUM: empty body treated as withheld | Withholding follows the provider flag, not text length; test added |
| LOW: missing fake fault injection | `fail_next_fetch`, `fail_next_ack`, `fail_next_nack` added and tested |
| LOW: `aclose` absent from the Protocol | Added to `InboxProvider`; lifespan closes a container inbox when present |
| LOW: `new_reference` one-off wrapper returned duplicate codes | Removed; callers use an explicit `ReferenceSequence` |

### Second review round

| Finding | Resolution |
|---|---|
| HIGH: `Decimal("1e999999")` / sub-quantum values escaped as `InvalidOperation` or a zero NEED | `_validate` caps magnitude, catches `InvalidOperation`, rejects positive values below canonical resolution; NEED/SUPPLY pipelines clarify; tests cover both kinds and values |
| HIGH: failed send lost the message on retry | Result and pending draft are cached before the provider call and the message is marked complete after the domain commit; a retry re-sends the same draft with the same key; test asserts one need and one email |
| MEDIUM: live config only rejected SQLite | `urlsplit` scheme check requires `postgres`/`postgresql`; tests cover SQLite, MySQL, empty, and a valid PostgreSQL URL |
| MEDIUM: envelope-shaped empty body was still marked withheld | Withholding follows the provider flag; `text: ""` produces a readable empty `RawMessage`; full and envelope tests added |
| MEDIUM: partial match reply overstated the recommended quantity | Reply now reports the allocation quantity and states the requested quantity in a partial-match sentence; test covers 50 allocated of 120 requested |
| LOW: entity-conflict comment overpromised review | Comment now says conflicts are ignored here and review escalation belongs to Task 9 |

## Sponsor gates (blocked; no live result claimed)

`uv run python scripts/spike_sponsors.py --offline` currently exits 2 and
reports `configuration: blocked` because `RESILINET_APP_ENV` is `test`. The
real spike requires owner-supplied values through environment variables:

- **Agentboxd:** persistent inbox provisioning, a signed webhook delivery, and
  a threaded reply. Tests verify the documented HMAC formula, envelope and
  withheld shapes, reply path, and idempotency header against
  `https://agentboxd.com/docs/api` and `/docs/webhooks`; account access is not
  available here.
- **MojoAuth:** `POST /users/magiclink` and `/token/validate`; the callback
  parameter encoding still requires one real browser click. No encoding is
  assumed in code.
- **Momen:** editor-only; the scheduled-refresh interval, native map
  acceptance, and published-origin CORS are unverified. Checklist:
  `docs/momen-setup.md`.
- **OpenAI:** structured-output model availability/rate limit unverified. The
  adapter is implemented in Task 5; live mode intentionally fails fast until
  its required key is present.

Deferred to their owned tasks: durable database intake (Task 4), OpenAI adapter
(Task 5), geocoding/Nominatim (Task 6), clarification flow (Task 7), duplicate
detection (Task 9), public HTTP routes (Task 17), MojoAuth adapter and sessions
(Task 13), and `FakeAuthProvider` (Phase 3). None is represented as working.

Low-risk items deliberately left to their owning tasks: formula boundary
validation for future timestamps/lat-lon ranges (Task 9), claim/nack argument
range validation (Task 4/10), an explicit ignore policy for non-message webhook
events (Task 4), and a `created_at` fallback when a full message omits
`received_at` (Task 4).

## Privacy and secrets

- Fixtures use reserved `example.org` / `example.net` addresses and fictional
  place names; no real sender data is present.
- No provider key, webhook secret, session, or action token is stored in the
  repository. `.env.example` contains names and empty secret values only.
- Public projection tests assert sender addresses and provider message IDs are
  absent.

## Commit status

No commit was created. The plan's Execution Discipline says not to commit unless
the user asks, notwithstanding the suggested checkpoint messages. All work is in
the working tree for the owner's review.

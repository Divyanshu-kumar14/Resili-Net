# Phase 6 Evidence — Deployment, Smoke Probe, and Release Rehearsal

Status: Tasks 25–27 implemented and locally verified on 2026-11-22, except
the live sponsor legs, which are blocked on owner credentials and recorded
as blocked, never as passes. No infrastructure was created or mutated from
this workspace; no live Agentboxd, OpenAI/NVIDIA, MojoAuth, or Momen account
was used.

Environment: Python 3.11.16, SQLite 3.53.1, PostgreSQL 18.7 (local Docker,
`docker-compose.test.yml`), Node 26.13.1, Render CLI v2.38.0, osv-scanner
2.7.1. No new Python dependencies; no lockfile change (`uv sync --frozen`
is clean).

## Commands and results

| Command | Result |
|---|---|
| `uv run pytest -q` | **486 passed, 1 skipped** (the pre-existing live-contract gate) |
| `uv run pytest tests/postgres -q` | **4 passed** (disposable local PostgreSQL) |
| `uv run ruff check src scripts tests` | **All checks passed** |
| `uv run mypy src` | **Success: no issues found in 50 source files** |
| `uv run python scripts/evaluate.py` | **pass**: 80/80 fields (100%), 10/10 pairs, 30/30 outcomes, p50 24.2 ms/message |
| `uv run python scripts/replay_demo.py` | 30 messages, needs_total 12, pages 200/200/200, **0 external connection attempts**, p50 19.4 ms |
| `uv run python scripts/verify_public.py` (seeded scratch SQLite) | **exit 0**, `ok: true`, `findings: []`, revalidation 304, lite transfer 10,361 bytes |
| `render blueprints validate render.yaml` | **valid: true** (0 actions) |
| `uv run python scripts/smoke_live.py --mode live` | **exit 2** with a redacted preflight report (no live settings here; no fake fallback) |
| `pnpm run audit` | **PASSED**: axe zero serious/critical on 5 pages, Lighthouse **100/100** on `/replay` and `/replay/lite` |
| `osv-scanner` (`uv.lock`) | **No issues found** |
| `osv-scanner` (`pnpm-lock.yaml`) | same transitive Lighthouse-tooling findings as Phase 5, disposition unchanged (local audit tooling only) |

Baseline before this phase was 466 passed, 1 skipped (467 total). The suite
is now 487 total: +11 production-config tests, +5 release-manifest tests,
+5 review-hardening tests (embedded wildcards, no-fake guards, Momen
preflight).

## Task 25 — Render Blueprint and production configuration

- `tests/integration/test_production_config.py` (11 tests, written first;
  the first run failed exactly on the missing `RESILINET_OPENAI_API_KEY`
  Blueprint entry): wildcard live CORS rejected, `Secure`/`HttpOnly`/
  `SameSite=Lax` cookies in live mode, live secrets required per selected
  provider, non-PostgreSQL live URLs rejected, no `seed_demo` reference
  anywhere under `src/resilinet` (startup cannot auto-seed), live settings
  build live adapters (never fakes) and replay settings build fakes even
  with keys present, plus two Blueprint shape tests and a smoke-preflight
  refusal test.
- `render.yaml`: added the missing `RESILINET_OPENAI_API_KEY` (`sync: false`)
  so switching `RESILINET_LLM_PROVIDER` needs no Blueprint edit. Everything
  else was already correct and is now pinned by tests: one `web`/`python`
  service + managed Postgres in `oregon`, `healthCheckPath: /api/v1/health`,
  `DATABASE_URL` via `fromDatabase`, secrets strictly `sync: false` with no
  committed values, migrations in the start command. `preDeployCommand` is
  deliberately absent: Render documents it as paid-compute-only, and this
  service targets the free plan — the test and `docs/deployment.md` record
  where to move the migration if the service is upgraded.
- `docs/deployment.md` (new): resources, secret table, owner-approval deploy
  steps, one-shot `provision.py`, worker modes, free-plan limits, rollback,
  and the no-destructive-command rule.
- `src/resilinet/config.py` needed no change: every validated production
  concern (wildcard CORS, live secrets incl. MojoAuth, PostgreSQL-only live
  URLs, `Secure` cookies) already fails fast with tests. Blast radius:
  `Settings` has wide downstream use; no signature was touched.
- `.github/workflows/test.yml` already runs frozen install, ruff, mypy, and
  the full suite against a PostgreSQL 18 service (first green run 45019756211
  on the Phase 5 push); unchanged.

## Task 26 — live smoke probe and docs

- `scripts/smoke_live.py` (new): refuses any `app_env != live`, preflights
  live secrets/PostgreSQL/https/test-recipient, checks `/health` + `/ready`,
  requires the incident to exist and be flagged `simulation`, provisions the
  inbox idempotently, privacy-scans the public projections, and optionally
  polls for an owner-sent probe marker to measure reply-to-map latency. The
  report redacts the recipient local-part and never prints a secret. Without
  credentials it exits 2 naming the blocker; it cannot construct a fake.
- Live run: **blocked** (no credentials in this workspace). The honest
  datum is the refusal itself plus the still-healthy production deployment
  from Phase 5 (`/api/v1/health`, `/api/v1/ready` ok on `resilinet-web`).
- `README.md`: status now covers Phases 1–6 work, deploy section points at
  `docs/deployment.md` and the smoke command, checks section names the new
  gate suites.

## Task 27 — rehearsals and release gate

- `tests/e2e/test_release_checklist.py` (5 tests, written first; all failed
  before the manifest existed): corpus counts, metric thresholds, clean
  privacy scan, green command records, and an honesty rule — the manifest
  must contain at least one `blocked` live entry with a reason, so live
  gates cannot be silently marked passed.
- `docs/evidence/release-checklist.md` (new): the manifest with today's
  measured values plus the blocked live entries.
- Rehearsals: cold offline replay rehearsed today from a scratch database
  (migrate → `seed_demo.py` → `replay_demo.py` → `verify_public.py`, all
  green); the live golden-path rehearsal is blocked on credentials and the
  demo script says so, including the explicit "switching to recorded replay"
  transition.
- `docs/demo-script.md`: pre-presentation boxes updated with today's
  re-verified figures; only the live rehearsal remains open.

## Independent review

A read-only hostile review of the Phase 6 diff was performed by the `review`
subagent (deployment configuration requires review regardless of diff size).
Verdict: CHANGES_REQUIRED with 9 findings; every finding was reproduced
against the code and resolved before this commit:

| # | Severity | Finding | Disposition |
|---|---|---|---|
| 1 | High | Release manifest cited a stale suite count (477 vs 482) the manifest test does not parse | **Fixed.** Manifest, demo script, and this file all cite the final 486 passed / 1 skipped. |
| 2 | High | Two `pass` live entries rested on Phase 5 evidence, which the manifest test accepts on a non-empty string | **Fixed.** Both entries now scope their evidence precisely (deploy health = last verified check; CI = latest green run on the Phase 5 push, no newer push in Phase 6) with blocked live legs recorded as blocked. |
| 3 | Medium | Smoke report echoed the owner `--expect-text` marker and stored raw health/ready bodies | **Fixed.** Marker withheld from the report; bodies replaced by status + privacy findings. Probe markers must be random non-PII tokens (flag help text). |
| 4 | Medium | Smoke privacy scan matched key names only, not string values | **Fixed.** Email-like and token-like values are flagged by path-and-kind (never the value), mirroring `verify_public.py`. |
| 5 | Medium | CORS validator rejected only an exact `"*"`; `https://*.evil.com` passed | **Fixed.** Any `*` in any origin is rejected (Starlette would fail it closed anyway; the allow-list now states exactness). |
| 6 | Medium | Task 26 checkbox overclaimed preflight coverage (mailbox, redirect) | **Fixed.** Preflight now also gates on `RESILINET_MOMEN_ORIGINS`; the checkbox describes exactly what is gated where (mailbox via idempotent live provisioning, redirect derived from the gated https base URL). |
| 7 | Medium | No-fakes path under-tested; `Settings()` failure in smoke exited via traceback | **Fixed.** Composition `RuntimeError` guards tested for both providers + inbox; invalid `Settings` exits 2 naming fields only, never values. |
| 8 | Low | No-seed test was a substring grep | **Fixed.** Now asserts no `import`/`from` of `seed_demo` or `provision` anywhere under `src/resilinet`. |
| 9 | Low | `preDeployCommand` rationale uncited; plan Interfaces still prescribed it | **Fixed.** Render docs cited in `docs/deployment.md`; plan Interfaces updated to the free-plan shape. |

## Unresolved gates

| Gate | Why it is unresolved |
|---|---|
| Live sponsor golden path (email → reply → map) | No Agentboxd/OpenAI-or-NVIDIA/MojoAuth/Momen credentials; smoke preflight exits 2 by design. |
| Reply-to-map ≤5 s on the published page | No published Momen page (Task 19 account gate still unmet). |
| Live golden-path rehearsal | Same credential gate; offline replay is rehearsed, live is not. |
| Screen-reader manual pass | No screen reader on this host (carried from Phase 5). |

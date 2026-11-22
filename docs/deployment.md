# ResiliNet Deployment Guide

Status: validated locally on 2026-11-22 (`render blueprints validate
render.yaml` → `valid: true`; `tests/integration/test_production_config.py`
11 passed). No infrastructure was created from this workspace; every step
below that mutates external state needs explicit owner approval first.

## What the Blueprint defines

`render.yaml` (repo root) declares one web service plus one managed
PostgreSQL database, both in `oregon`:

- `resilinet-web`: `type: web`, `runtime: python`, `plan: free`. Build:
  `pip install uv && uv sync --frozen --no-dev`. Start:
  `uv run alembic upgrade head && uv run uvicorn resilinet.main:app
  --host 0.0.0.0 --port $PORT`. Health check: `/api/v1/health`;
  readiness (database + in-app worker heartbeat) is `/api/v1/ready`.
- `resilinet-db`: managed PostgreSQL, `plan: free`. The service receives it
  as `RESILINET_DATABASE_URL` via `fromDatabase.connectionString`.
- Free-plan constraint: `preDeployCommand` is a paid-compute feature (Render
  documents it as paid-only; see <https://render.com/docs/blueprint-spec> and
  the Rails blueprint guide at <https://render.com/docs/deploy-rails-6-7>),
  so the migration stays in the start command. If the service moves to a paid
  plan, prefer `preDeployCommand: uv run alembic upgrade head` and drop the
  migration from the start command (`tests/integration/test_production_config.py`
  pins the current free-plan shape and says where to change it).

The web process also runs the durable worker (`RESILINET_RUN_WORKER_IN_APP`
defaults to `true`) because Render's free plan offers no background worker
service. On a paid plan, deploy a worker service running
`uv run python scripts/run_worker.py` and set `RESILINET_RUN_WORKER_IN_APP`
to `false` on the web service. Graceful shutdown is handled in the app
lifespan (worker stop flag, inbox close, engine dispose).

## Secrets (never committed)

All secret values are `sync: false` and are entered in the Render dashboard:

| Variable | Purpose |
|---|---|
| `RESILINET_AGENTBOXD_API_KEY` | persistent inbox provisioning, replies |
| `RESILINET_AGENTBOXD_WEBHOOK_SECRET` | signed webhook verification |
| `RESILINET_MOJOAUTH_API_KEY` | magic-link login (required in live mode) |
| `RESILINET_NVIDIA_API_KEY` | extraction/critic when `RESILINET_LLM_PROVIDER=nvidia` |
| `RESILINET_OPENAI_API_KEY` | extraction/critic when `RESILINET_LLM_PROVIDER=openai` |
| `RESILINET_NOMINATIM_CONTACT_EMAIL` | geocoding request identification |

Non-secret values are pinned in the Blueprint (`RESILINET_APP_ENV=live`,
`RESILINET_LLM_PROVIDER=nvidia`,
`RESILINET_NVIDIA_MODEL`/`RESILINET_NVIDIA_BASE_URL`,
`RESILINET_INCIDENT_KEY=demo-incident`,
`RESILINET_PUBLIC_BASE_URL=https://resilinet-web.onrender.com`). Update
`RESILINET_PUBLIC_BASE_URL` if Render assigns a different hostname, and set
`RESILINET_MOMEN_ORIGINS` to the published Momen origin once the Task 19
account gate is resolved (currently unset: no browser CORS).

## Deploy (owner approval required)

1. Owner confirms workspace/resource names and approves infrastructure
   creation plus the production migration.
2. Create a Blueprint from this repository in the Render dashboard; fill the
   six `sync: false` secrets above.
3. Deploy. Migrations run automatically on each start (free plan).
4. Provision once against the live database (creates the incident row and the
   persistent Agentboxd inbox, prints the webhook URL — never a credential):

   ```bash
   render jobs create <web-service-id> \
     --start-command "uv run python scripts/provision.py"
   ```

5. Register the printed webhook URL for `message.received`,
   `message.enriched`, and `message.released` in Agentboxd; copy its signing
   secret into `RESILINET_AGENTBOXD_WEBHOOK_SECRET`.
6. Verify: `GET /api/v1/health` → `{"status":"ok"}`;
   `GET /api/v1/ready` → database `ok`, worker `ok` (or `external`).
7. Run the live smoke probe (needs owner test accounts; without credentials
   it exits 2 naming the blocker and uses no fakes):

   ```bash
   uv run python scripts/smoke_live.py --mode live --recipient tester@example.org
   ```

## Operating notes

- Free web services sleep after inactivity and free Postgres expires after 30
  days; use paid plans for a stable webhook and demo.
- Never run destructive seed/migration commands against a non-local database
  without explicit owner approval. `scripts/seed_demo.py` is local/demo only
  and is never invoked by application startup
  (`test_app_startup_performs_no_demo_seed`).
- Rollback: redeploy the previous green commit from the Render dashboard;
  migrations are forward-only (`alembic downgrade` is a local/dev tool).
- Delete-on-request: remove the sender's stored messages and reports; see
  `README.md` (Privacy).

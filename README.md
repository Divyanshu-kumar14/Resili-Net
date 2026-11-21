# Resili-Net

Emergency relief coordination over plain email. Volunteers send a need or a
stock update; ResiliNet extracts it into structured data, proposes a match
against available inventory, replies with a reference code, and publishes a
privacy-safe map.

A proposal is a recommendation. Stock is reserved, not dispatched. Only an
authorized confirmation commits stock, and only an explicit delivery report
consumes it.

## Status

Phases 1–2 are locally verified: durable idempotent intake, a relational model
with Alembic migrations, a lease-based job runner, strict extraction with a
deterministic prompt-injection audit, gazetteer-first cached geocoding, a
one-round clarification flow, deterministic matching with a bounded critic,
transactional stock reservations, and threaded recommendation replies with
private coordinator confirmation grants. Secure action execution, the public
map API and Momen UI, and formal process supervision/HA land in later phases; a
live worker entrypoint and Render Blueprint are present. The live OpenAI or
NVIDIA NIM, Nominatim, Agentboxd, and MojoAuth account gates remain unproven.
See `docs/architecture.md`, `docs/evidence/phase0.md`,
`docs/evidence/phase1.md`, and `docs/evidence/phase2.md`.

## Requirements

- Python 3.11
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
cp .env.example .env
uv sync --frozen
uv run alembic upgrade head
uv run uvicorn resilinet.main:app --reload
curl -s localhost:8000/api/v1/health
```

`RESILINET_APP_ENV` selects `test`, `replay`, or `live`. `live` requires the
provider secrets listed in `.env.example` and refuses to start without them.
`replay` never makes external network calls.

`RESILINET_LLM_PROVIDER` selects `openai` (Responses API) or `nvidia` (NVIDIA
NIM chat completions). Both adapters share the same extraction and critic
prompts and validate tool output with the same Pydantic schemas.

### Live webhook reachability

Agentboxd delivers only to a public HTTPS URL (port 443 or 1024–65535) and
refuses private, loopback, and single-label hosts. For local live testing,
expose the app through a tunnel, for example:

```bash
cloudflared tunnel --url http://localhost:8000
```

Set `RESILINET_PUBLIC_BASE_URL` to the tunnel origin, register
`https://<host>/internal/agentboxd/events` for `message.received`,
`message.enriched`, and `message.released`, and copy its signing secret into
`RESILINET_AGENTBOXD_WEBHOOK_SECRET`. The webhook only queues jobs; the worker
process below consumes them. MojoAuth is not required for this path; its
adapter and action pages are Phase 3 work.

### Provision and run the worker

```bash
uv run alembic upgrade head
uv run python scripts/provision.py
uv run python scripts/run_worker.py
```

`provision.py` creates the incident row and the persistent Agentboxd inbox, then
prints the webhook URL to register. `run_worker.py` processes queued messages
and delivers the outbox. The FastAPI process does not start it automatically.

### Deploy on Render

`render.yaml` defines a free Postgres database and web service. Render's free
plan does not offer background worker services, so the blueprint sets
`RESILINET_RUN_WORKER_IN_APP=true` and the web process runs the same durable
worker as `scripts/run_worker.py`. Create a Blueprint from this repository,
then enter the `sync: false` secrets (`RESILINET_AGENTBOXD_API_KEY`,
`RESILINET_AGENTBOXD_WEBHOOK_SECRET`, `RESILINET_NVIDIA_API_KEY`,
`RESILINET_NOMINATIM_CONTACT_EMAIL`) in the Render dashboard. After the web
service is live, provision once:

```bash
render jobs create <web-service-id> --start-command "uv run python scripts/provision.py"
```

If you deploy a paid Render worker service instead, set
`RESILINET_RUN_WORKER_IN_APP=false` on the web service and run
`uv run python scripts/run_worker.py` there. Free web services sleep after
inactivity and free Postgres expires after 30 days; use paid plans for a stable
webhook and demo. Update `RESILINET_PUBLIC_BASE_URL` if Render assigns a
different hostname than `resilinet-web.onrender.com`.

## Checks

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
```

## Sponsors

- **Agentboxd** — persistent incident inbox, signed inbound webhooks, threaded replies.
- **MojoAuth** — passwordless sender verification and operator sign-in.
- **Momen** — public map and accessible list views.
- **OpenAI or NVIDIA NIM** — schema-constrained extraction and a bounded
  adversarial critic. Select with `RESILINET_LLM_PROVIDER` (`openai` or
  `nvidia`); the adapter validates the model's tool arguments against the same
  Pydantic schema.

## Privacy

Public responses never contain sender addresses, action tokens, sessions, raw
messages, or provider payloads. Demo data is fictional and labelled as a
simulation. Delete-on-request: remove the sender's stored messages and
reports; see the deployment guide in `docs/`.

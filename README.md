# ResiliNet

[![CI](https://github.com/Divyanshu-kumar14/Resili-Net/actions/workflows/test.yml/badge.svg)](https://github.com/Divyanshu-kumar14/Resili-Net/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/python-3.11-3776AB.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688.svg)](https://fastapi.tiangolo.com/)

**Emergency relief coordination over plain email.**

During a disaster, volunteers and shelters often have no app, a weak
connection, and a phone that can still send email. ResiliNet turns those emails
into a coordinated relief operation. A volunteer writes "we need 50 gallons of
water at the school on 5th Street". ResiliNet extracts the need, matches it
against depot stock, reserves supplies, replies in the same thread with a
reference code, and publishes the need on a privacy-safe public map.

- **No app to install.** Every participant action starts from an email.
- **Safe by design.** A proposal is only a recommendation. Stock is reserved,
  never dispatched automatically. Only a verified, authorized confirmation
  commits stock, and only an explicit delivery report consumes it.
- **Works offline for demos.** Replay mode runs the full pipeline on a
  recorded corpus with zero external network calls.

## Contents

- [How it works](#how-it-works)
- [Features](#features)
- [Tech stack](#tech-stack)
- [Quick start](#quick-start)
- [Demo](#demo)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Testing and quality](#testing-and-quality)
- [Project structure](#project-structure)
- [Security and privacy](#security-and-privacy)
- [Project status](#project-status)
- [Documentation](#documentation)
- [License](#license)

## How it works

```text
Volunteer email
      │
      ▼
Agentboxd incident inbox ──signed webhook──▶ FastAPI intake
                                               │ verify HMAC, store message + job
                                               ▼
                                        Durable worker
                                               │ extract (LLM, strict schema)
                                               │ geocode (gazetteer → Nominatim)
                                               │ clarify once, or record need/supply
                                               │ match + critic review
                                               │ reserve stock (one transaction)
                                               ▼
                                         Outbox worker
                                               │
              ┌────────────────────────────────┼───────────────────────────┐
              ▼                                ▼                           ▼
   Threaded reply with RN-/AL- refs   Secure action links       Public map and list
                                      (MojoAuth session)        (Momen, /replay, /lite)
```

1. **Intake.** Agentboxd delivers each email as a signed webhook. ResiliNet
   verifies the HMAC over the raw body, then stores the message and its job in
   one transaction. Duplicate deliveries are ignored.
2. **Extraction.** An LLM (OpenAI or NVIDIA NIM) turns the email into a
   structured report. A strict Pydantic schema validates the output before any
   data changes. Email text is treated as data, never as instructions.
3. **Clarification.** If a quantity or location is missing, ResiliNet asks the
   sender one follow-up question instead of guessing.
4. **Matching.** A deterministic scorer ranks depots by distance, available
   stock, and freshness. A rules critic and a bounded LLM critique check the
   proposal before stock is reserved.
5. **Confirmation.** Depots and operators confirm, cancel, or report delivery
   through single-use action links that also require a verified session.
6. **Publishing.** A public API exposes an allow-listed projection of needs and
   allocations for the map and an accessible list view.

See [`docs/architecture.md`](docs/architecture.md) for trust boundaries,
transaction rules, and the scoring formulas.

## Features

| Area | What ResiliNet does |
|---|---|
| Intake | Idempotent signed webhooks, per-sender and per-incident rate limits, quote and signature stripping |
| Extraction | Schema-constrained LLM output, unit canonicalisation, deterministic prompt-injection audit |
| Geocoding | Curated gazetteer first, then cached Nominatim lookups |
| Matching | Deterministic scoring, rules critic, bounded LLM critique, duplicate-report corroboration |
| Inventory | Transactional reservations with sorted row locks; no over-reservation under concurrency |
| Identity | Passwordless sender verification, incident-scoped roles, CSRF-protected single-use action grants |
| Reliability | Lease-based job runner, exponential backoff, dead letters, operator review queue, durable outbox |
| Public view | Privacy-safe JSON API, Momen map contract, offline replay mirror, no-JavaScript lite view (~10 KB) |
| Accessibility | axe: zero serious or critical issues; Lighthouse accessibility 100 on local pages |

## Tech stack

- **Backend:** Python 3.11, FastAPI, SQLAlchemy 2 (async), Alembic, Pydantic
- **Database:** PostgreSQL in production, SQLite for local development and tests
- **AI:** OpenAI Responses API or NVIDIA NIM, selected with `RESILINET_LLM_PROVIDER`
- **Hosting:** Render (Blueprint in [`render.yaml`](render.yaml))
- **Tooling:** uv, pytest, Ruff, mypy (strict), Playwright, axe-core, Lighthouse

### Sponsor integrations

| Sponsor | Role in ResiliNet |
|---|---|
| **Agentboxd** | Persistent incident inbox, signed inbound webhooks, threaded replies |
| **MojoAuth** | Passwordless sender verification and operator sign-in |
| **Momen** | Public map and accessible list views |
| **OpenAI / NVIDIA NIM** | Schema-constrained extraction and an adversarial critic |

## Quick start

Requirements: Python 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Divyanshu-kumar14/Resili-Net.git
cd Resili-Net
cp .env.example .env
uv sync --frozen
uv run alembic upgrade head
uv run uvicorn resilinet.main:app --reload
```

Check that the service is up:

```bash
curl -s localhost:8000/api/v1/health
```

## Demo

The offline replay runs the whole pipeline on a 30-message fictional disaster
corpus. It needs no accounts and fails if any external connection is attempted.

```bash
uv run python scripts/replay_demo.py
```

To browse the result:

```bash
RESILINET_APP_ENV=replay uv run uvicorn resilinet.main:app --port 8000
# open http://localhost:8000/replay and http://localhost:8000/replay/lite
```

The five-minute walkthrough (clarification, matching, duplicate and injection
handling, public view, failure recovery) is in
[`docs/demo-script.md`](docs/demo-script.md).

## Configuration

All settings are environment variables prefixed with `RESILINET_`. The full
list with comments is in [`.env.example`](.env.example).

| Variable | Purpose |
|---|---|
| `RESILINET_APP_ENV` | `test`, `replay` (no network calls), or `live` (requires provider secrets) |
| `RESILINET_LLM_PROVIDER` | `openai` or `nvidia` |
| `RESILINET_PUBLIC_BASE_URL` | Public HTTPS origin used for webhook and action links |
| `RESILINET_RUN_WORKER_IN_APP` | Run the worker inside the web process (default `true`) |

`live` mode refuses to start without the secrets for the selected providers.

### Running live

Agentboxd only delivers to a public HTTPS URL. For local live testing, expose
the app through a tunnel:

```bash
cloudflared tunnel --url http://localhost:8000
```

Set `RESILINET_PUBLIC_BASE_URL` to the tunnel origin, then provision the inbox
and start the worker:

```bash
uv run python scripts/provision.py   # creates the incident and inbox, prints the webhook URL
uv run python scripts/run_worker.py  # processes jobs and delivers the outbox
```

Register `https://<host>/internal/agentboxd/events` for `message.received`,
`message.enriched`, and `message.released`, and put its signing secret in
`RESILINET_AGENTBOXD_WEBHOOK_SECRET`.

## Deployment

[`render.yaml`](render.yaml) defines a Postgres database and a web service on
Render. Render's free plan has no background workers, so the web process runs
the durable worker itself (`RESILINET_RUN_WORKER_IN_APP=true`).

1. Create a Blueprint on Render from this repository.
2. Enter the `sync: false` secrets in the Render dashboard:
   `RESILINET_AGENTBOXD_API_KEY`, `RESILINET_AGENTBOXD_WEBHOOK_SECRET`,
   `RESILINET_NVIDIA_API_KEY`, and `RESILINET_NOMINATIM_CONTACT_EMAIL`.
3. Provision once after the service is live:

   ```bash
   render jobs create <web-service-id> --start-command "uv run python scripts/provision.py"
   ```

4. Run the live smoke probe. It exits with code 2 and names the missing
   setting when credentials are absent; it never falls back to fakes.

   ```bash
   uv run python scripts/smoke_live.py --mode live --recipient tester@example.org
   ```

Free Render services sleep when idle and free Postgres expires after 30 days;
use paid plans for a stable demo. The full runbook is in
[`docs/deployment.md`](docs/deployment.md).

## Testing and quality

```bash
uv run pytest -q                 # 486 tests
uv run ruff check src scripts tests
uv run mypy src                  # strict mode
uv run python scripts/evaluate.py
```

| Check | Result |
|---|---|
| Extraction field accuracy (recorded corpus) | 80/80 fields |
| Duplicate-report detection | 10/10 pairs |
| Pipeline outcomes | 30/30 messages |
| Accessibility (axe, serious or critical) | 0 issues |
| Lighthouse accessibility | 100 |

The extraction figures use deterministic recorded provider output. They measure
canonicalisation and persistence, not live model quality.

Optional suites:

- `uv run pytest tests/postgres -q` proves that concurrent workers cannot
  over-reserve stock. It needs a local PostgreSQL from
  [`docker-compose.test.yml`](docker-compose.test.yml).
- `pnpm run audit` runs the Playwright, axe, and Lighthouse accessibility audit.

GitHub Actions runs lint, type checks, and the full suite, including the
PostgreSQL tests, on every push.

## Project structure

```text
src/resilinet/
  adapters/      Agentboxd, MojoAuth, LLM, and geocoding clients, plus fakes for tests and replay
  api/           Public API and internal webhook routes
  domain/        Matching, priority, quantities, duplicates, and lifecycle rules
  services/      Intake, extraction, clarification, critic, allocation, and identity services
  workers/       Job runner and outbox delivery
  web/           Replay mirror, lite view, action pages, and operator review queue
alembic/         Database migrations
scripts/         Provisioning, worker, demo, evaluation, and smoke-test scripts
fixtures/        Fictional demo incident, message corpus, and gazetteer
frontend/momen/  Momen setup notes and API contract
tests/           Unit, integration, end-to-end, and PostgreSQL suites
docs/            Architecture, deployment, demo script, and phase evidence
```

## Security and privacy

- Public responses never contain sender addresses, action tokens, sessions,
  raw messages, or provider payloads.
- Webhooks are rejected unless the HMAC over the raw body verifies and the
  timestamp is within 300 seconds.
- Verifying an email address proves control of the address, not authority.
  Depot and operator rights come from incident-scoped memberships.
- An action link alone cannot change state. The request also needs a verified
  session, a CSRF token, a role check, and single-use consumption.
- Logs redact action tokens and drop query strings.
- All demo data is fictional and labelled as a simulation.
- To delete a sender's data on request, remove their stored messages and
  reports; see [`docs/deployment.md`](docs/deployment.md).

## Project status

The full pipeline is implemented and verified locally and in CI, and the
service is deployed on Render. The following live integrations have not yet
been proven end to end with real accounts:

- the live golden path through Agentboxd, MojoAuth, and the LLM provider;
- the published Momen map page, which is waiting on editor access.

Until then, demos use the offline replay. Details are in
[`docs/evidence/release-checklist.md`](docs/evidence/release-checklist.md).

## Documentation

- [Architecture and trust boundaries](docs/architecture.md)
- [Deployment runbook](docs/deployment.md)
- [Demo script](docs/demo-script.md)
- [Momen setup](docs/momen-setup.md)
- [Glossary](GLOSSARY.md)
- [Release checklist](docs/evidence/release-checklist.md) and phase evidence in [`docs/evidence/`](docs/evidence/)

## License

Released under the [MIT License](LICENSE).

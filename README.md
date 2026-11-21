# Resili-Net

Emergency relief coordination over plain email. Volunteers send a need or a
stock update; ResiliNet extracts it into structured data, proposes a match
against available inventory, replies with a reference code, and publishes a
privacy-safe map.

A proposal is a recommendation. Stock is reserved, not dispatched. Only an
authorized confirmation commits stock, and only an explicit delivery report
consumes it.

## Status

Phase 0 scaffold, locally verified: the Fake golden path, adapter contracts,
durable-intake seams, and privacy-safe projection are implemented. The durable
pipeline, identity, actions, and public HTTP views land in later phases; live
sponsor account gates are still blocked. See `docs/architecture.md` and
`docs/evidence/phase0.md`.

## Requirements

- Python 3.11
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
cp .env.example .env
uv sync --frozen
uv run uvicorn resilinet.main:app --reload
curl -s localhost:8000/api/v1/health
```

`RESILINET_APP_ENV` selects `test`, `replay`, or `live`. `live` requires the
provider secrets listed in `.env.example` and refuses to start without them.
`replay` never makes external network calls.

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
- **OpenAI** — schema-constrained extraction and a bounded adversarial critic.

## Privacy

Public responses never contain sender addresses, action tokens, sessions, raw
messages, or provider payloads. Demo data is fictional and labelled as a
simulation. Delete-on-request: remove the sender's stored messages and
reports; see the deployment guide in `docs/`.

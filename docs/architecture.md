# ResiliNet Architecture and Trust Boundaries

Status: Phase 0 decisions. Sponsor facts below were verified against public
documentation on 2026-11-21. Account-only capabilities are marked **UNRESOLVED**
and must be proven with `scripts/spike_sponsors.py` before they are described
as live.

## Executable flow

```text
internet sender
      |
      v
Agentboxd persistent incident inbox
      |  signed envelope webhook (or claim/ack fallback)
      v
FastAPI /internal/agentboxd/events
      |  verify raw-body HMAC + timestamp, then parse
      v
durable intake transaction: Message + Job (+ provider idempotency key)
      |
      v
supervised worker
      |-- fetch withheld/envelope content when absent
      |-- extract (OpenAI or NVIDIA NIM structured output, or Fake/Replay)
      |-- geocode (gazetteer -> cached Nominatim -> none)
      |-- clarify once, or persist Report/Need/Supply
      |-- deterministic matching + rules critic + bounded LLM critique
      |-- reservation transaction (sorted locks, fresh availability)
      |-- outbox row (in the same transaction)
      v
outbox worker -> Agentboxd threaded reply / private action email
      |
      +--> public read API -> Momen map/list and FastAPI /replay, /lite
      +--> private action pages -> verified session + CSRF + single-use grant
```

## Trust boundaries

1. **Email content is untrusted data.** It is delimited for the model, never
   executed as instructions, and never reaches tools. Extraction output is
   validated by a strict Pydantic schema before any domain change.
2. **The webhook is unauthenticated until the raw body verifies.** HMAC is
   checked over the exact bytes before JSON parsing; timestamps more than 300
   seconds from now are rejected.
3. **Verification proves control of an email address, not authority.** Depot
   and operator authority come from incident-scoped memberships. An unrelated
   verified sender cannot overwrite stock or confirm another participant's
   allocation.
4. **Action links are bearer capabilities with a second factor.** A one-time
   token alone cannot mutate state: the POST also needs a matching verified
   session, CSRF token, a role/target check, and transactional consumption.
5. **Public output is an allow-list projection.** No sender address, action
   token, session, raw message, provider payload, private note, or unavailable
   precise coordinate is serialized.
6. **Replay is a separate mode.** `RESILINET_APP_ENV=replay` constructs only
   recorded/Fake providers and makes zero non-loopback network calls.

## Sponsor contracts (verified public facts)

### Agentboxd

Sources: <https://agentboxd.com/docs/api>, <https://agentboxd.com/docs/webhooks>.

- Persistent inbox: `POST /v1/inboxes` with `client_id` is idempotent and
  returns the existing inbox. Temporary inboxes are receive-only and are not
  used.
- Envelope webhook: `X-Mailroom-Timestamp` plus `X-Mailroom-Signature =
  hex(HMAC-SHA256(secret, "<timestamp>.<raw body>"))`. Envelope bodies carry
  `inbox_id, thread_id, message_id, from, to, subject, labels, received_at`
  and no content. Withheld mail also requires `GET /v1/messages/:id`.
- Threaded reply: `POST /v1/inboxes/:id/messages/:messageId/reply` sets
  `In-Reply-To` and `References`; the adapter never accepts an arbitrary
  `in_reply_to` argument.
- Idempotency: send, reply, draft create, and draft send honour
  `Idempotency-Key`.
- Work queue: `claim` leases inbound messages (`lease_seconds` 30–3600);
  `ack` is idempotent; `nack` supports delayed retry; `409 lease_expired`
  means the work may have been redelivered; ten claims without an ack
  dead-letter the message.
- Not relied on: realtime stream, attachment extraction, agent runtime.

### MojoAuth

Sources: <https://mojoauth.com/docs/api-reference/>,
<https://mojoauth.com/docs/configurations/redirection/>.

- `POST https://api.mojoauth.com/users/magiclink` with `x-api-key` accepts
  `email`, optional `language`, and optional `redirect_url`; returns
  `state_id`.
- `POST https://api.mojoauth.com/token/validate` validates a token.
- **UNRESOLVED (account-only):** the exact callback query/payload parameter
  that carries the token, whether `state_id` is returned on the redirect, and
  the response shape of `/token/validate`. Do not guess the encoding. The
  callback remains disabled in live mode until the spike records the observed
  request from a real test sender.

### Momen

Sources: <https://docs.momen.app/docs/actions/guide/api_integration/>,
<https://docs.momen.app/docs/code/code_component/>.

- External JSON APIs are configured as reusable API resources; inputs and
  response schemas become bindable. Secrets must be bound in an Actionflow,
  never in page/frontend configuration.
- **UNRESOLVED (editor-only):** scheduled-refresh intervals (target 1,000 ms;
  5,000 ms is the fallback), native map marker/label/selection/accessibility
  support, and published-origin CORS. The map path is selected only after the
  spike; see `docs/momen-setup.md`.

### OpenAI

- Structured output uses `AsyncOpenAI.responses.parse(..., text_format=Schema)`
  with a Pydantic schema, temperature 0 where supported, and no tools.
  `output_parsed is None` is a failure, not an empty report.
- **UNRESOLVED (account-only):** model availability and rate limits for the
  hackathon account.

### NVIDIA NIM

- NIM exposes the OpenAI-compatible `POST /chat/completions` surface. Its
  hosted schema documents `tools` but not `tool_choice` or `response_format`,
  so the adapter asks for one `emit_result` function, validates the returned
  JSON arguments with the same Pydantic schema, and accepts a JSON-content
  fallback. A schema-invalid answer gets one deterministic repair attempt.
- Selected with `RESILINET_LLM_PROVIDER=nvidia`; `RESILINET_NVIDIA_MODEL` and
  `RESILINET_NVIDIA_BASE_URL` are configurable. Live mode requires only the
  selected provider's key.
- **UNRESOLVED (account-only):** tool-calling reliability per NIM model and
  rate limits for the hackathon account.

## Runtime invariants

```text
reserved_remaining = sum(max(allocation.quantity - allocation.delivered_quantity, 0)
                         for allocation in PROPOSED or CONFIRMED)
delivered_total = sum(allocation.delivered_quantity for every non-rejected allocation)
remaining_need = need.quantity - delivered_total - reserved_remaining   # >= 0
stock_ratio = min(1, supply.available / remaining_need)                 # only if > 0
freshness = 1 for age <= 6 h; linear to 0 at 24 h; 0 after
score = 0.6 * (1 - distance_km / 50) + 0.25 * stock_ratio + 0.15 * freshness
priority = urgency_weight * 10 + min(headcount, 200) / 10 + age_hours * 0.5
           + (20 if medical escalation else 0)
```

`available = on_hand - sum(active quantity - delivered_quantity)`. It is never
clamped: a negative value is an invariant violation, not zero. Allocations move
`PROPOSED -> CONFIRMED -> DELIVERED`; `CANCELLED` releases the remaining
reservation; `REJECTED` is a critic/operator refusal. A proposal is never a
dispatch or delivery.

## Transaction boundaries

- Phase 0 caches the committed processing result and pending draft by provider
  message ID. A failed send is retried with the same deterministic outbox key
  without re-running extraction or reservations. The durable outbox in later
  tasks replaces this cache; the key rule stays the same.
- Intake verifies the raw body, then atomically inserts the message and its
  durable job. Duplicate provider events return success without a new job.
- LLM and outbound HTTP calls happen outside database transactions.
- Reservation commit locks need and candidate supply rows in sorted ID order,
  reloads availability, writes allocations/events/outbox in one transaction.
- Snapshot, need revision, confirmation, cancellation, and delivery lock their
  aggregate and re-check role, token, quantity, and state in that transaction.
- SQLite tests prove behavior; PostgreSQL tests prove concurrent workers cannot
  over-reserve.
- **Locked re-reads must refresh.** Any path that reads a row unlocked and
  later re-reads it under `FOR UPDATE` in the same transaction must use
  `populate_existing`; otherwise SQLAlchemy returns the stale identity-map
  snapshot and state checks run against pre-lock state. Task 22 proved the
  failure mode (a cancellation and a delivery both "winning") and the fix in
  `AllocationService._locked_need`/`_locked_trio`. The concurrency suite is
  `tests/postgres/` against a disposable local PostgreSQL
  (`docker-compose.test.yml`, scratch databases named `resilinet_scratch_*`,
  created and dropped by the suite, name verified before dropping). It must
  never be pointed at a developer or production database.

## Phase 1 durable path

```text
signed envelope -> POST /internal/agentboxd/events
  verify raw-body HMAC + 300 s timestamp window (400 on failure)
  resolve the incident from RESILINET_INCIDENT_KEY (404 if unprovisioned)
  one transaction:
    lock the incident row (serialises the rolling limits on PostgreSQL)
    sender upsert (case-insensitive identity)
    rolling limits: 20/sender/hour, 200/incident/hour -> one throttled notice
    Message insert, unique on (incident_id, provider_message_id) -> 204 duplicate
    quote/signature stripping into body_text; raw_text kept internally
    withheld content -> status deferred + fetch_content job
    otherwise -> status queued + process_message job (dedupe_key process:<id>)
```

- The worker claims one due job with a row lock (skip-locked), leases it, and
  runs the handler outside any transaction. A commit followed by a crash is
  recoverable because the handler re-checks message state under a lock and a
  retried job becomes a no-op. Expired leases are reclaimed; attempts back off
  exponentially up to a dead letter. Terminal writes are fenced on
  `(status=leased, lease_owner, attempts)`, so a stale worker cannot overwrite
  a job another worker reclaimed or finished.
- `fetch_content` fetches withheld mail, keeps the message deferred while the
  provider still marks it withheld, then normalizes it and enqueues exactly one
  `process_message` job. `process_message` extracts outside the transaction,
  records injection findings, then persists reports/entities, asks one
  clarification, merges one correction, or escalates to `ReviewCase` in a
  single transaction. An audit-write failure is logged and never masks the
  original job failure.
- Geocoding resolves in order: curated gazetteer (`exact`), cached Nominatim
  (`area`), `none`. The gazetteer is consulted before the cache and is not
  cached, so a negative cache entry cannot mask a later known place. Cache keys
  are `(incident_id, normalized location)`. Timeouts and malformed responses
  are not cached; definitive empty results are negatively cached. Replay mode
  constructs the adapter disabled and makes no request.
- A clarification question lists every missing field and maps model field
  names (`items`) onto mergeable fields. A correction is merged only for those
  fields, only from the original sender, and only once, and is re-checked for
  plausibility and confidence. A second correction, a wrong sender, or a reply
  that arrives after the report left `clarification_pending` becomes a review
  case without changing the completed report. Clean NEED/SUPPLY reports
  intentionally queue no acknowledgement until Task 12's threaded
  recommendation.
- Outbound rows are delivered with a stable `outbox:<uuid>` idempotency key. A
  row left in `sending` by a crash is reclaimed after a stale window; the
  provider key makes that replay safe. `scripts/run_worker.py` drives the
  message and outbox loops. `render.yaml` runs the same runtime inside the web
  process on Render's free plan (`RESILINET_RUN_WORKER_IN_APP=true`), or as a
  standalone worker on paid plans. High-availability supervision remains later
  work.

## Public identifiers and logging

- Public JSON uses stable UUIDs for incidents, entities, needs, and supplies;
  `RN-` identifies a need and `AL-` one allocation. The `need_id`/`supply_id`
  fields on an allocation are those public UUIDs, never provider message IDs.
- Structured request logs carry method, redacted path, status, duration, and
  trace ID. Query strings are never logged. `/actions/{token}` is recorded as
  `/actions/{token}`, and a `uvicorn.access` filter applies the same redaction
  and drops query strings from the default access log.
- No action token, session, sender address, or provider payload is serialized
  into public output or written to operational logs.

## Phase 0/1 scope note

Phase 0's synchronous composition through the real ports and domain schemas is
still present as the golden-path slice. Phase 1 added the durable intake,
job runner, extraction persistence, geocoding, and clarification path beside
it; the synchronous slice is replaced, not duplicated, in Phase 2.
`FakeAuthProvider` is deliberately deferred to Phase 3 with the MojoAuth
adapter.

## Unresolved account gates

See `docs/evidence/phase0.md` for the commands, results, and the blocked-gate
record.

| Gate | Owner action | Blocking? |
|---|---|---|
| Agentboxd persistent inbox + HMAC webhook + reply | provide `RESILINET_AGENTBOXD_API_KEY`, `RESILINET_AGENTBOXD_WEBHOOK_SECRET`, register the webhook URL | live mode only |
| MojoAuth magic link + callback encoding | optional until Phase 3; provide the key when the identity adapter lands | live identity |
| Momen scheduled refresh + native map | provide editor access, run the checklist in `docs/momen-setup.md` | live public map |
| OpenAI or NVIDIA NIM structured output | provide the selected key, confirm model quota and tool calling | live extraction |
| Render deployment | owner approval of resources and production migration | release |

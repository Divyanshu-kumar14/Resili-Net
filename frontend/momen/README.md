# Momen public map — recorded contract

This directory holds the read-only binding for the hosted Momen public
application. Nothing here runs in the ResiliNet service.

| File | Purpose |
|---|---|
| `api-contract.json` | Machine-checked contract: endpoints, filters, field allow-lists, marker rules, privacy forbiddens, refresh status. |
| `../../tests/fixtures/momen/map_snapshot.json` | Recorded public DTO produced by the real `map_snapshot` projection. |
| `../../tests/e2e/test_momen_contract.py` | Proves the fixture equals the live projection and that the contract makes no unverified claim. |

## What is verified locally

- The fixture equals, as parsed JSON, what
  `resilinet.services.public.map_snapshot` returns for the same rows, so the
  contract cannot drift from the DTO silently. (The HTTP layer around that
  projection — filters, ETag, status codes — is covered separately by
  `tests/integration/test_public_api.py`.)
- Every declared collection field set equals the fixture's field set exactly.
- The three endpoint paths exist on the app, are `GET`, and the map route exposes
  exactly the `resource`, `urgency`, and `status` query parameters.
- CORS middleware is installed when `RESILINET_MOMEN_ORIGINS` is configured.
- No forbidden field, email-like value, action path, token, or session appears in
  the bound payload.
- The refresh block records `verified: false`; a 5 s poll is never described as
  passing the ≤5 s reply-to-map gate.

Run it:

```bash
uv run pytest tests/e2e/test_momen_contract.py -q
```

## What is NOT verified

The Momen editor spike is **blocked on account access**. There is no owner
editor session for the target workspace, so none of the following can be
observed or claimed:

- the shortest scheduled-refresh interval the editor actually accepts;
- whether the native map component accepts arbitrary markers with
  shape + label + colour;
- the published page URL, its CORS origin, and the publish policy for assets;
- measured p50/p95 reply-visible → marker/list-visible latency;
- the hosted `/lite` transfer size with JavaScript disabled.

Do not fill in `verified_interval_ms` or flip `verified` in
`api-contract.json` without that measurement. The local replay mirror at
`/replay` and `/replay/lite` is the substitute demonstration; it is not evidence
for the hosted page.

## Binding rules

1. `PUBLIC_BASE_URL` is a plain variable. No API key, no action token, no
   session cookie, no provider credential is bound into a page or frontend
   action.
2. Only the three `GET` endpoints are bound. There is no write path from the
   public page; operator actions stay behind `/operator/login` on the service.
3. Markers are one per entity. Entities without coordinates remain visible in
   the list and tables; they are never dropped silently.
4. Quantity basis (`stated` vs `estimated`) is displayed as text, not colour.
5. The simulation label and the freshness banner are always rendered, never
   dismissible.

See `../../docs/momen-setup.md` for the full editor and publish checklists.

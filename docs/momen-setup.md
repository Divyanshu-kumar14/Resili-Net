# Momen Public Map Setup

The public application binds the FastAPI public projection. It is read-only and
receives no secret.

## Selected path (pending the editor spike)

- **Preferred:** native external JSON API + native map component.
- **Fallback:** a documented Momen React/TypeScript code component built with
  the `momen-cli` workflow. Inspect `momen-cli --help` before running commands;
  do not guess the CLI surface. Leaflet/OSM is a candidate only if its build
  succeeds inside that component and its assets are allowed by the publish
  policy.

The native map is accepted only if all of these work:

1. Arbitrary markers (not a fixed set of pins).
2. Marker label, shape, and color together (never color alone).
3. Keyboard-accessible marker selection with an accessible name.
4. A detail view equivalent to the list row.
5. No external font/tile/script dependency that breaks the offline claim; the
   hosted Momen page is not claimed to work offline.

## API binding

Create an API collection with a variable for `PUBLIC_BASE_URL`
(`https://<service>/`). Add one GET resource per endpoint:

| Name | Method | Path |
|---|---|---|
| `map` | GET | `/api/v1/incidents/{incident_id}/map` |
| `needs` | GET | `/api/v1/incidents/{incident_id}/needs` |
| `summary` | GET | `/api/v1/incidents/{incident_id}/summary` |

Inputs: `incident_id` (path), `resource`, `urgency`, `status` (query, optional).
The response schema is the public DTO in `src/resilinet/services/public.py`.
Do not bind an API key or action URL into a page or frontend action. These
endpoints are public, GET-only, and credential-free.

## Refresh spike

1. Configure a Scheduled Job that re-runs the `map` API.
2. Record the shortest interval the editor actually accepts and the observed
   end-to-end delay from a database change to a rendered marker/list row.
3. Target: 1,000 ms. If only 5,000 ms works, record that the ≤5 s gate is
   **unmet**; do not describe a 5 s poll as passing it.
4. If a documented event-driven refresh exists, evaluate it with the same
   measurement.

Record: editor project, interval, sample count, p50/p95 delay, failures.

## Page construction checklist

- [ ] Simulation label visible and not dismissible.
- [ ] Freshness banner ("Updated N s ago") from `generated_at`.
- [ ] Resource, urgency, and status filters with visible labels.
- [ ] Map markers use shape + label + color.
- [ ] Equivalent table/list reachable by a visible toggle; same rows.
- [ ] Detail panel: entity, resource, quantity (stated vs estimated), status,
      reference, last updated, approximate distance.
- [ ] Empty, unverified, review, and no-coordinate states are announced in text.
- [ ] `aria-live="polite"` announcements for new matches without stealing focus.
- [ ] Keyboard-only: filters, markers, table, detail, toggle.
- [ ] 200% text zoom and a narrow viewport do not clip content.
- [ ] `prefers-reduced-motion` and high-contrast preference respected.
- [ ] No action tokens, sessions, sender addresses, or provider payloads in
      bound data or page source.

## Publish checklist

- [ ] `frontend/momen/api-contract.json` matches the recorded map fixture.
- [ ] `tests/e2e/test_momen_contract.py` passes against the fixture.
- [ ] Published test URL uses the same public base URL and CORS origin.
- [ ] Golden-path sample measured reply-visible -> marker/list-visible with a
      sample count, not a single reading.
- [ ] Redacted screenshot and timing evidence saved under `docs/evidence/`.
- [ ] `/lite` on the Momen host is under 100 KB transferred for the seeded
      response and works with JavaScript disabled.

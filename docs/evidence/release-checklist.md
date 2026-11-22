# Release Checklist

Status: Phase 6 rehearsal manifest, written 2026-11-22 from fresh local runs.
Live sponsor gates remain blocked on owner credentials and are recorded as
`blocked` with reasons — no live result is claimed. Consumed by
`tests/e2e/test_release_checklist.py`; do not edit the manifest by hand to
make that suite pass.

## Rehearsals

- Cold offline replay (this workspace, 2026-11-22): scratch SQLite seed
  (`seed_demo.py`: 10 entities, 7 stock, 2 needs), `replay_demo.py` over the
  30-message corpus (0 external connection attempts, pages 200/200/200),
  `verify_public.py` against the seeded DB (`ok: true`, no findings, lite
  transfer 10,361 bytes, ETag revalidation 304). No Momen, MojoAuth,
  Agentboxd, or OpenAI account was used.
- Live golden path (phone email → recommended estimate → RN/AL refs → map;
  depot confirmation; duplicates; injection; medical review): **blocked** —
  no live credentials in this workspace. `scripts/smoke_live.py --mode live`
  exits 2 at preflight and constructs no fake provider as a substitute.

## Manifest

```json
{
  "version": 1,
  "date": "2026-11-22",
  "corpus": {
    "messages": 30,
    "duplicate_pairs": 10
  },
  "metrics": [
    {
      "name": "extraction_field_accuracy",
      "value": 100,
      "threshold": 90,
      "sample_size": 80,
      "status": "pass",
      "note": "deterministic fakes: canonicalisation and persistence fidelity, not live-LLM quality"
    },
    {
      "name": "duplicate_pairs",
      "value": 10,
      "threshold": 10,
      "sample_size": 10,
      "status": "pass"
    },
    {
      "name": "pipeline_outcomes",
      "value": 30,
      "threshold": 30,
      "sample_size": 30,
      "status": "pass"
    },
    {
      "name": "axe_serious_critical",
      "value": 0,
      "threshold": 0,
      "sample_size": 5,
      "status": "pass",
      "note": "local pages via pnpm run audit; published-Momen page excluded (account gate)"
    },
    {
      "name": "lighthouse_accessibility",
      "value": 100,
      "threshold": 90,
      "sample_size": 2,
      "status": "pass",
      "note": "local /replay and /replay/lite; hosted Momen page unaudited (account gate)"
    }
  ],
  "offline_replay": {
    "messages": 30,
    "needs_total": 12,
    "external_connection_attempts": 0,
    "public_pages_status": [200, 200, 200],
    "pipeline_ms_p50": 19.4
  },
  "privacy": {
    "public_projection_findings": [],
    "verify_public_ok": true,
    "lite_transferred_bytes": 10361,
    "revalidation_status": 304
  },
  "commands": [
    {"command": "uv run pytest -q", "exit": 0, "result": "486 passed, 1 skipped (live-contract gate)"},
    {"command": "uv run pytest tests/postgres -q", "exit": 0, "result": "4 passed"},
    {"command": "uv run ruff check src scripts tests", "exit": 0, "result": "All checks passed"},
    {"command": "uv run mypy src", "exit": 0, "result": "no issues in 50 source files"},
    {"command": "uv run python scripts/evaluate.py", "exit": 0, "result": "pass: 80/80 fields, 10/10 pairs, 30/30 outcomes"},
    {"command": "uv run python scripts/replay_demo.py", "exit": 0, "result": "30 messages, 0 external attempts, pages 200/200/200"},
    {"command": "uv run python scripts/verify_public.py", "exit": 0, "result": "ok: true, findings: []"},
    {"command": "render blueprints validate render.yaml", "exit": 0, "result": "valid: true"},
    {"command": "osv-scanner (uv.lock)", "exit": 0, "result": "No issues found"},
    {"command": "pnpm run audit", "exit": 0, "result": "axe zero serious/critical; Lighthouse 100/100 (local pages)"}
  ],
  "live": [
    {
      "name": "live_sponsor_golden_path",
      "status": "blocked",
      "reason": "no Agentboxd/OpenAI-or-NVIDIA/MojoAuth/Momen credentials in this workspace"
    },
    {
      "name": "live_smoke_preflight",
      "status": "blocked",
      "reason": "scripts/smoke_live.py --mode live exits 2 naming the missing live settings; no fake fallback",
      "evidence": "exit 2 with redacted preflight report, verified 2026-11-22"
    },
    {
      "name": "reply_to_map_latency",
      "status": "blocked",
      "reason": "no published Momen page exists, so reply-to-map latency is unmeasured (Task 19 gate unmet)"
    },
    {
      "name": "production_deploy_health",
      "status": "pass",
      "evidence": "2026-11-22: deployed resilinet-web answers /api/v1/health and /api/v1/ready (database + worker heartbeat ok)"
    },
    {
      "name": "github_actions_ci",
      "status": "pass",
      "evidence": "run 45019756211 succeeded 2026-11-22 on the Phase 5 push (frozen sync, ruff, mypy, full suite incl. PostgreSQL concurrency)"
    }
  ]
}
```

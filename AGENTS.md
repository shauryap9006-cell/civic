# AGENTS.md — CivicLens

Read this file first on every task. It is short on purpose. Details live in `docs/`.

## 1. What we are building (30 seconds)

CivicLens turns an Android phone (mounted as a dashcam) into a pothole reporter that also **verifies repairs**.

1. The phone runs a detector while driving and creates a signed **event** for each pothole (road-only crop + GPS + time).
2. The backend merges events into one **pothole**, emails the authority once (to a **test inbox**), and shows it on a public map.
3. The authority clicks a **Fixed** link. That is only a **claim**.
4. Later drives upload **passes** (where the camera went, and whether image quality was good enough to see the road).
5. After the claim, enough **valid clean passes** that saw no pothole make it `verified`. Any new detection makes it `reopened`, with before/after evidence.

The differentiator is step 5: *not seeing a pothole counts as evidence only when the camera could really have seen it.*

This is a hackathon MVP. Finish one reliable end-to-end slice before adding anything else.

## 2. Source of truth (when documents disagree)

Priority, highest first:

1. `docs/STATE_MACHINE.md` (statuses, transitions, verification rules)
2. `openapi.yaml` and `db/schema.sql` (contracts and data model)
3. `config/thresholds.yaml` (every tunable number)
4. `docs/PRD.md` (requirements, with IDs such as `VER-05`)
5. `docs/FEATURES.md`, `docs/WORKFLOWS.md`, `docs/ARCHITECTURE.md`
6. `docs/TASKS.md`, `docs/TEST_PLAN.md`, `docs/SCREENS.md`, `docs/DATA_AND_MODEL.md`, `docs/DEMO_SCRIPT.md`

If you find a conflict or a gap: **do not guess silently.** Choose the safest interpretation, implement it, and append an entry to `docs/OPEN_QUESTIONS.md` (what conflicted, what you chose, why). Never edit a higher-priority file to match lower-priority code without asking.

## 3. Repository layout (target)

```
civiclens/
  AGENTS.md  CLAUDE.md  README.md  Makefile  .env.example  docker-compose.yml  openapi.yaml
  config/thresholds.yaml
  db/schema.sql  db/seed.sql
  docs/                    all specifications (this pack)
  core/                    Python package civiclens_core: detector wrapper, tracker, gate, event builder,
                           quality sampler, signing, outbox, replay tool, golden fixtures
  backend/                 FastAPI app (app/), tests (tests/)
  web/                     Next.js website (map, detail, Fixed page, impact, /demo)
  android/                 Kotlin app (CameraX, LiteRT, foreground service, Room outbox)
  ml/                      datasets, labelling guide, training and export scripts, eval scripts
  tools/                   small scripts (export_thresholds.py, make_scenario.py)
```

## 4. Commands (the contract; keep them working)

```
make up              # PostGIS + MinIO
make db-reset        # wipe and re-apply schema.sql and seed.sql
make test            # core + backend + web tests
make lint
make replay VIDEO=... GPS=...      # push a recorded drive through the core as a replay device
make demo-reset      # reset demo state through the admin endpoint
```

If you add a new way to run or test something, add it to the `Makefile` and this section.

## 5. Tech stack (fixed — do not swap without an entry in `docs/DECISIONS.md`)

| Part | Choice |
|---|---|
| Detector | Ultralytics YOLO (n or s) fine-tuned; export ONNX / LiteRT (INT8 or FP16). Licence caveat in `docs/DECISIONS.md` (D-08) |
| Tracking | ByteTrack (BoT-SORT as alternative) |
| Core language | Python 3.11+ (`core/`) for replay and golden tests; Kotlin port for Android |
| Android | Kotlin, CameraX, LiteRT, foreground service, Room, libsodium or Tink for Ed25519 |
| Backend | Python, FastAPI, Pydantic v2, async SQLAlchemy, asyncpg, httpx |
| Database | PostgreSQL 16 + PostGIS (schema in `db/schema.sql`) |
| Storage | S3-compatible (MinIO locally; Cloudflare R2 or Supabase Storage deployed) |
| Email | Resend (or `console` provider in development) |
| Web | Next.js (App Router), TypeScript, Tailwind, MapLibre GL |
| Tests | pytest (+ pytest-asyncio), Vitest or Jest for web, JUnit for Android |

**Do not introduce:** Kafka, Kubernetes, microservices, GraphQL, blockchain, a message broker, an ORM other than SQLAlchemy, a second web framework, or any model trained from scratch. Do not add a library when the standard library or an existing dependency does the job.

## 6. Hard rules (violating any of these is a bug)

1. **Test inbox only.** With `EMAIL_MODE=test` every email goes to `TEST_INBOX`, never to an authority address. Never send to real people.
2. **No wide frames.** Only the road-region crop is saved or uploaded (`event.store_wide_frame` must stay `false`).
3. **No hard-coded thresholds.** Every tunable number comes from `config/thresholds.yaml`. Tests that need a specific value override it explicitly.
4. **Time comes from the Clock.** Server code never calls `datetime.now()` directly; use `clock.now()` (real time + `clock_offset_seconds`). Otherwise the demo's skip-time feature breaks. Signature timestamp checks use the **real** clock.
5. **Idempotency everywhere.** Events and passes are keyed by device-generated UUIDs; re-sending must never create duplicates, duplicate emails or duplicate history rows.
6. **A claim is not proof.** `claimed_fixed` never counts as repaired. Only `verified` does, and only via valid clean passes.
7. **Never fake evidence.** Seeded or replayed data must be marked (`devices.platform` = `seed` or `replay`) and shown with a badge in the UI. Never present simulated results as measured.
8. **Severity is graded, not measured.** Never display depth, volume or any physical measurement.
9. **Secrets only in environment variables.** Never commit `.env`, keys or tokens. Never log Fixed tokens, signatures' private keys or `ADMIN_SECRET`.
10. **Verify signatures before trusting any payload.** Reject unsigned or tampered events and passes with a stable error code.
11. **State changes only through `state_machine.py`.** No other code writes `potholes.status`. Every change writes a `status_history` row with evidence.
12. **Driver safety.** The Android app must never require attention while the vehicle moves.

## 7. Working agreements

- **One task per session.** Take the next unblocked ticket from `docs/TASKS.md`. State the ticket ID and the requirement IDs at the start and in the commit message (for example `T-29 VER-05 VER-06: verification evaluator`).
- **Tests first for the logic that matters:** state machine, pass coverage, verification, merge, signature checks, outbox. The cases are named in `docs/TEST_PLAN.md`; implement those names.
- **Prove it works.** After each task run the relevant tests and report the actual output. Do not claim "done" without a passing run. If you could not run something, say exactly what and why.
- **Report format after each task:** (1) what changed (files), (2) requirement IDs covered, (3) tests run and results, (4) assumptions made, (5) anything left or risky.
- **Small, reviewable changes.** Prefer new modules with clear names over editing many files. No drive-by refactors.
- **Keep docs true.** If behaviour changes, update the relevant doc in the same change, respecting section 2.

## 8. Code conventions

- Python: type hints everywhere, `ruff` for lint and format, no wildcard imports, pure functions for decision logic (coverage, verification, severity) so they are trivially testable. I/O lives at the edges.
- Decision logic functions take plain data in and return plain data out (no database sessions inside them).
- API errors use the `Error` schema in `openapi.yaml` with stable snake_case codes.
- Logging: structured, one line per decision (signature rejected, event merged, email sent, verification decision with the evidence used). Never log secrets.
- Web: TypeScript strict, no `any` without a comment, server components for data fetching, status shown with colour **and** icon/label.
- Kotlin: coroutines, no work on the main thread beyond UI, Room for the outbox, all thresholds from the generated `thresholds.json` asset.
- Commit style: `T-<ticket> <REQ-IDs>: <summary>`.

## 9. Domain vocabulary

- **Event**: one confirmed pothole sighting from one device (UUID from the device).
- **Pothole**: merged record of events within `merge.radius_m`.
- **Pass**: one drive session with 5 Hz quality samples.
- **Crossing**: the moment a pass comes closest to a pothole.
- **Valid clean pass**: a pass whose approach window to a pothole met every quality check and saw no detection of it.
- **Claim**: the authority says it is fixed. **Verified**: the claim confirmed by valid clean passes. **Reopened**: new evidence contradicts it.

## 10. Definition of done (MVP)

The script in `docs/DEMO_SCRIPT.md` runs end to end from `make db-reset` without manual fixes; every `TC-*` test in `docs/TEST_PLAN.md` marked **P0** passes; `make test` is green; numbers for E1–E6 exist or are explicitly marked "not done".

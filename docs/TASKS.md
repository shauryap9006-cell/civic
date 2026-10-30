# TASKS

Status: v0.1. Ordered tickets with dependencies, requirement IDs and acceptance. Take the next unblocked ticket. One ticket per session. Update the status column as you go (`todo`, `doing`, `done`, `blocked`).

Tracks: **F** foundation · **C** core and model · **B** backend · **W** website · **A** Android · **E** evaluation and demo.
Suggested split for three people: Person 1 = A; Person 2 = B (+ F); Person 3 = C and W. If solo: follow gate order and cut Android polish before verification.

Size: S ≤ 2 h · M ≤ half a day · L ≤ a day (rough).

---

## Phase 0 — Foundation

| ID | Track | Ticket | Size | Depends | Reqs | Acceptance | Status |
|---|---|---|---|---|---|---|---|
| T-01 | F | Scaffold repo folders (`core/`, `backend/`, `web/`, `android/`, `ml/`, `tools/`), `pyproject.toml` files, linters, pre-commit | S | — | NFR-12 | `make lint` runs (even if empty) | done |
| T-02 | F | Bring up infra: `make up`; verify `schema.sql` and `seed.sql` apply; MinIO bucket exists | S | T-01 | NFR-12 | `psql` shows all tables; MinIO console shows `civiclens-crops` | done |
| T-03 | B | Config loader (`.env` + `thresholds.yaml`, typed, validated), `Clock` with offset from `app_state`, `/healthz` | S | T-02 | NFR-05 | `GET /healthz` returns db, storage, clock offset; invalid threshold file fails at startup | done |

## Gate G0: infra up, `/healthz` green.

---

## Phase 1 — Pure logic first (no database, no network)

| ID | Track | Ticket | Size | Depends | Reqs | Acceptance | Status |
|---|---|---|---|---|---|---|---|
| T-10 | B | `logic/geo.py` (haversine distance, bearing, heading difference, weighted mean location) | S | T-01 | MRG-02 | Unit tests against known coordinate pairs | done |
| T-11 | B | `logic/severity.py` | S | T-03 | EVT-04 | `TC-SEV-01..02` | done |
| T-12 | B | `logic/merge.py` (choose pothole or create) | S | T-10 | MRG-01 | `TC-MRG-01..04` | done |
| T-13 | B | `logic/coverage.py` (crossings, approach window, per-sample checks, reasons) | L | T-10 | VER-02, VER-03 | `TC-COV-01..14`; test factories for synthetic passes exist | done |
| T-14 | B | `logic/state_machine.py` (allowed transitions, guards, reason codes) | M | T-03 | VER-04..08 | Illegal transitions raise; table in STATE_MACHINE §2 fully covered | done |
| T-15 | B | `logic/verification.py` (`evaluate`, pure, idempotent) | L | T-13, T-14 | VER-05..08 | `TC-VER-01..17` (scenarios S1..S16) green | done |

## Gate G1: all logic tests green with no database.

---

## Phase 2 — Backend slice

| ID | Track | Ticket | Size | Depends | Reqs | Acceptance | Status |
|---|---|---|---|---|---|---|---|
| T-20 | B | Models and DB layer mirroring `schema.sql`; `services/potholes.py` skeleton with row locking | M | T-03 | API-01 | Create/read potholes in a transaction test | done |
| T-21 | B | Device registration and signature verification (headers + envelope), timestamp window | M | T-20 | API-02, EVT-03 | `TC-ING-01..06` | done |
| T-22 | B | Upload signing and storage helpers (presigned PUT/GET, existence and hash check) | S | T-02 | EVT-02 | Upload a test JPEG; hash mismatch is detected | done |
| T-23 | B | `POST /v1/events` ingest: validate, dedupe, merge, severity, history `first_event`, per-event results | L | T-12, T-11, T-21, T-22 | EVT-01..03, MRG-01 | `TC-ING-*`, `TC-MRG-*` through the API; duplicate id → `duplicate` | done |
| T-24 | B | Mailer: templates, console + Resend providers, `reports` table, T2 transition, one initial email per pothole | M | T-23 | RPT-01..04 | `TC-RPT-01..04`; ten merged events → one email | done |
| T-25 | B | Fixed token derivation (HMAC, epoch) + `GET/POST /v1/fixed/{token}` with the claim rules | M | T-14, T-24 | VER-04, API-03 | `TC-VER-18`, `TC-VER-19`, `TC-API-05` | done |
| T-26 | B | `POST /v1/passes` ingest, path building, coverage computation and cache | L | T-13, T-21 | VER-01, VER-02 | `TC-PASS-*`, `TC-COV-*` through the API | done |
| T-27 | B | `services/potholes.evaluate` wiring: after events, passes, claims; history rows with evidence; reopened email | L | T-15, T-24, T-26 | VER-05..08 | `TC-VER-01..17` end to end against the database; no duplicate history rows | done |
| T-28 | B | Public read API: `GET /v1/potholes`, `/v1/potholes/{id}` (detail with evidence), `/v1/stats` | M | T-27 | WEB-01..04 | Contract test against `openapi.yaml` | done |
| T-29 | B | Escalation scheduler + `simulate-time` + `reset` (demo-only guard) | M | T-27 | ESC-01..03 | `TC-ESC-01..03`, `TC-DEMO-01..02` | done |

## Gate G2 (the spine): upload a synthetic event + pass through the API → pothole appears → email (console) → claim → verified/reopened exactly as in the scenarios.

---

## Phase 3 — Core and model (parallel to Phase 2)

| ID | Track | Ticket | Size | Depends | Reqs | Acceptance | Status |
|---|---|---|---|---|---|---|---|
| T-30 | C | Assemble dataset: public pothole sets (check licences) + hard negatives; labelling guide applied; route-based split | L | T-01 | DET-03 | `ml/DATASET.md` lists sources, licences, counts, split | todo |
| T-31 | C | Train baseline YOLO n/s; log metrics; export ONNX and LiteRT (INT8/FP16); record accuracy drop after quantization | L | T-30 | DET-03, DET-04 | PR curve and export files saved; E1 table filled | todo |
| T-32 | C | `civiclens_core`: detector wrapper, ByteTrack, gate, event builder, signing | L | T-31 | DET-01, DET-02, EVT-01..03 | `TC-DET-01`; events validate against `EventPayload` | todo |
| T-33 | C | Quality sampler (sharpness, luma, occlusion, detector_active, inference_fps) | M | T-32 | VER-01 | `TC-DET-03` golden values | todo |
| T-34 | C | Outbox (SQLite) + uploader (crop → event → pass, backoff, idempotent) | M | T-32, T-23 | OFF-01..03 | `TC-OFF-02`, `TC-OFF-03` | todo |
| T-35 | C | Replay tool: video + GPX → events and passes → upload as a `replay` device | L | T-33, T-34 | ESC-03 | `make replay` produces a pothole on the map | todo |
| T-36 | C | Golden fixtures (frames/detections → expected events; frames → expected metrics) for the Android port | M | T-32, T-33 | DET-05 | Fixtures committed under `core/tests/golden/` | todo |

## Gate G3: `make replay` of a real recorded drive creates potholes on the backend with correct crops.

---

## Phase 4 — Website (starts after T-28 contract is stable; can mock with openapi examples earlier)

| ID | Track | Ticket | Size | Depends | Reqs | Acceptance | Status |
|---|---|---|---|---|---|---|---|
| T-40 | W | Next.js scaffold, API client generated from `openapi.yaml`, layout, design tokens (status colours + icons) | M | T-01 | WEB-01 | Pages render with mocked data | done |
| T-41 | W | Map page with six status styles, legend, filters, popups, replay/seed badges | L | T-40, T-28 | WEB-01 | `TC-WEB-01`, `TC-WEB-02` | done |
| T-42 | W | Pothole detail page: timeline, before/after, pass log table, "occlusion not checked" note | L | T-40, T-28 | WEB-02 | `TC-WEB-03` | todo |
| T-43 | W | Authority Fixed page (preview, button, states for 404/410/409) | M | T-25, T-40 | WEB-03 | `TC-WEB-05` | todo |
| T-44 | W | Impact page and header strip | S | T-28 | WEB-04 | `TC-WEB-04` | todo |
| T-45 | W | `/demo` page: skip-time buttons, reset, service status (demo mode only) | S | T-29 | WEB-05 | `TC-DEMO-03` | todo |

## Gate G4: the full path works on a laptop: replay → map → email → Fixed click → verified → reopen with before/after.

---

## Phase 5 — Android

| ID | Track | Ticket | Size | Depends | Reqs | Acceptance | Status |
|---|---|---|---|---|---|---|---|
| T-50 | A | Project scaffold, permissions, CameraX preview, consent screen | M | T-01 | APP-01, APP-05, APP-06 | Preview works; consent gate blocks start | todo |
| T-51 | A | Model runtime (LiteRT) on frames; measure FPS and temperature for 15–20 min | L | T-31, T-50 | DET-01, NFR-01 | Sustained FPS recorded (E3) or fallback decision logged in `DECISIONS.md` | todo |
| T-52 | A | Kotlin tracker + gate + event builder; pass golden fixtures; generate `thresholds.json` via `make export-thresholds` | L | T-36, T-51 | DET-05, DET-02 | `TC-DET-02` | todo |
| T-53 | A | GPS + heading + speed + quality sampler (5 Hz samples) | M | T-52 | VER-01 | Golden metric tolerances met (`TC-DET-03`) | todo |
| T-54 | A | Foreground service, start/stop only when stationary, dimmed status view, crash-safe pass finalisation | L | T-53 | APP-02, APP-04 | `TC-APP-01..03` | todo |
| T-55 | A | Ed25519 keys (libsodium/Tink + Keystore wrap), registration, request and payload signing | M | T-21 | EVT-03, API-02 | Backend accepts a phone-signed event (`TC-ING-01` in the field) | todo |
| T-56 | A | Room outbox + uploader (crop → event → pass), backoff, idempotency | L | T-55 | OFF-01..04 | `TC-OFF-01..03` on the device | todo |
| T-57 | A | Screens: drive, settings, trip summary | M | T-54 | APP-01, APP-03 | Matches `docs/SCREENS.md` | todo |
| T-58 | A | Thermal management | S | T-54 | DET-06 | Pass samples show reduced `inference_fps` while throttled | todo |

## Gate G5: a real phone drive uploads events and a pass; the pothole appears on the map.

---

## Phase 6 — Evaluation and demo

| ID | Track | Ticket | Size | Depends | Reqs | Acceptance | Status |
|---|---|---|---|---|---|---|---|
| T-60 | E | Build the replay set: labelled passes (clean, blurred, occluded, dark, throttled) over known potholes; some real, some scripted; all labelled | M | T-35 | VER-* | `ml/replay_set/README.md` lists each case and whether it is real or simulated | todo |
| T-61 | E | Evaluation scripts E1–E6, results table with counts, charts (gated vs naive absence) | L | T-60 | NFR-01, metrics | `ml/results/` with a dated report | todo |
| T-62 | E | Demo seed and reset; demo route data; email template polish | S | T-29 | WEB-05 | `make demo-reset` returns a clean state | todo |
| T-63 | E | Record the backup demo video; prepare slides' real-versus-simulated table | S | T-62 | — | Video stored on two devices | todo |
| T-64 | E | Full rehearsal against `docs/DEMO_SCRIPT.md` with timing; fix findings | M | T-62 | all P0 | Script runs from reset without manual fixes (twice in a row) | todo |
| T-65 | E | Pi 5 portability run (optional) | M | T-36 | DET-07 | `TC-DEMO-04` | todo |

## Gate G6 (done): definition of done in `AGENTS.md` §10.

---

## Cut order if time runs short (cut from the top)

1. T-65 Pi portability
2. T-58 thermal management (keep the honest `inference_fps` in samples)
3. T-44 impact page
4. T-29 escalation scheduler (keep `simulate-time` only if the demo shows escalation)
5. T-56 offline demo polish (keep basic upload)
6. Android polish in T-57

**Never cut:** T-13, T-14, T-15, T-26, T-27 (verification), T-43 (Fixed page), T-42 (evidence page), T-60/T-61 (honest numbers).

## Parallelism map

- After G1: B continues T-20..T-29 while C does T-30..T-36 and W starts T-40 with mocks.
- A starts T-50 immediately (no dependencies except the scaffold) and T-51 as soon as an exported model exists (a pretrained YOLO nano is fine to measure FPS before the trained one is ready).

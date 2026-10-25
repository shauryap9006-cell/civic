# ARCHITECTURE

Status: v0.1. Companion to `docs/PRD.md`. Contracts: `openapi.yaml`, `db/schema.sql`, `config/thresholds.yaml`.

## 1. System overview

```
┌──────────────────────────── DEVICE (Android phone / replay tool / later Linux camera) ───────────────────────────┐
│ Camera → Detector → Tracker → Gate (≥ min_track_frames) → Event builder → Signer → Outbox (SQLite/Room)         │
│ GPS + IMU + frame quality → Pass sampler (5 Hz) → Pass builder → Signer → Outbox                               │
└───────────────────────────────────────────────────┬─────────────────────────────────────────────────────────────┘
                                                    │ HTTPS: presigned crop PUT, signed events, signed passes
                                                    ▼
┌──────────────────────────────────────────── BACKEND (FastAPI) ───────────────────────────────────────────────┐
│ auth → ingest → merge → severity → state_machine ← verification ← coverage                                    │
│                        │                                   ▲                                                  │
│                        ▼                                   │                                                  │
│                  mailer (Resend) ← escalation job     clock (real + offset)                                   │
└───────────────┬───────────────────────────────┬───────────────────────────────────────┬──────────────────────┘
                ▼                               ▼                                       ▼
        PostgreSQL + PostGIS            Object storage (crops)                     Email (test inbox)
                ▲
                │ public read API
┌───────────────┴────────────── WEBSITE (Next.js + MapLibre) ──────────────────────────────────────────────────┐
│ / map · /pothole/[id] evidence · /fixed/[token] authority page · /impact · /demo (demo mode only)           │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## 2. Design principles

1. **Decision logic is pure.** Merge, severity, coverage, verification and the state machine are pure functions over plain data. The database is a thin shell around them. This makes them easy to test exhaustively and easy for an agent to get right.
2. **Recompute, don't increment.** Verification is a deterministic function of (pothole, events, passes, thresholds, now). It can be re-run at any time and must give the same answer (see `TC-VER-17`). This makes out-of-order uploads (offline queues) safe.
3. **Device-generated ids and signed payloads.** Retries are harmless; tampering is detectable.
4. **Evidence over assertion.** Every status change stores the event ids, pass ids and threshold values that justified it.
5. **Privacy by construction.** Only a road crop leaves the device. No wide frames, no faces or plates by design.
6. **One config file.** Thresholds are data, not code.
7. **Portable core.** The detection-to-event logic is the same on every device; the platform wrapper supplies camera, GPS and storage.

## 3. Components and responsibilities

### 3.1 Device side

| Module | Responsibility | Platform notes |
|---|---|---|
| Camera input | Frames at reduced resolution for inference; ROI crop from `detection.roi` | CameraX `ImageAnalysis` (Android); OpenCV or libcamera (Python/Linux) |
| Detector | Runs the exported model; returns boxes, confidence, class | LiteRT with GPU delegate (Android); ONNX Runtime (Python) |
| Tracker | ByteTrack: ids across frames | Port must match golden fixtures |
| Gate | Emits an event only when a track has `tracking.min_track_frames` frames and mean confidence ≥ `tracking.min_mean_confidence` | Pure logic, shared fixtures |
| Event builder | Picks best frame (`confidence × area`), crops the road region with padding, hashes the JPEG, assembles the payload | |
| Quality sampler | Every `pass_quality.sample_interval_s`: position, heading, speed, GPS accuracy, `sharpness`, `occlusion_ratio`, `mean_luma`, `detector_active`, `inference_fps` | Metric definitions in section 6 |
| Signer | Ed25519 signature of the exact payload string and of each request | Do not assume hardware-backed Ed25519 on Android; use libsodium or Tink and wrap the private key with an Android Keystore AES key |
| Outbox | Durable queue of crops, events and passes with states `pending`, `sent`, `failed`; exponential backoff with jitter | Room (Android), SQLite (Python) |
| Thermal manager | Lowers inference rate at `thermal.reduce_at_status`, pauses at `thermal.stop_at_status` | Android `PowerManager` thermal status |
| Control UI | Three screens (drive, settings, trip summary) | See `docs/SCREENS.md` |

### 3.2 Backend

Package layout (`backend/app/`):

```
main.py              app factory, lifespan (starts the escalation scheduler), routers
config.py            env + thresholds.yaml loader (typed, validated at startup)
clock.py             Clock: now() = real now + clock_offset_seconds (from app_state)
db.py                async engine/session helpers
models.py            SQLAlchemy models mirroring db/schema.sql
schemas.py           Pydantic models mirroring openapi.yaml
auth/                device registration, signature verification, admin secret, fixed-token HMAC
storage.py           presigned PUT/GET, object existence and hash check
ingest/events.py     event ingest pipeline
ingest/passes.py     pass ingest pipeline
logic/geo.py         pure geodesic helpers (distance, bearing, heading difference)
logic/merge.py       pure: choose pothole or create new; weighted location
logic/severity.py    pure: score and band
logic/coverage.py    pure: crossings and window checks for one (pass, pothole)
logic/verification.py pure: evaluate(pothole_view, events, coverage, now, thresholds) -> Decision
logic/state_machine.py pure: allowed transitions and guards; the only module that decides statuses
services/potholes.py applies Decisions to the database inside a transaction
services/mailer.py   builds and sends emails; records reports
services/escalation.py periodic job
api/                 routers: devices, uploads, events, passes, potholes, fixed, stats, admin
tests/               pytest; factories for synthetic passes and events
```

Rules:
- Anything in `logic/` imports no database, no network, no clock. Time and thresholds are arguments.
- `services/potholes.py` loads the data, calls `logic/*`, applies the returned `Decision` (status change, history row, side effects) under a row lock (`SELECT … FOR UPDATE` on the pothole row).
- Side effects (emails) are recorded as `reports` rows with `pending`, then sent by the mailer. A crash between decision and email must not lose or duplicate the email (one `initial` report per pothole is enforced by a check before insert inside the same transaction).

### 3.3 Website

Next.js App Router, server components fetch from the public API. MapLibre GL for the map with OSM-based tiles. No login. See `docs/SCREENS.md`.

### 3.4 Background work

No broker. Two mechanisms only:
1. **Inline after ingest:** after an event or pass is stored, the request handler calls `services/potholes.evaluate(pothole_id)` for the affected potholes and triggers the mailer.
2. **Periodic scheduler** started in the FastAPI lifespan: runs the escalation check every `escalation.check_interval_minutes` and retries `pending`/`failed` reports (up to `email.max_retries`). `POST /v1/admin/simulate-time` calls the same functions directly.

## 4. Data flow summary

| Flow | Path |
|---|---|
| Detect → report | frame → detector → tracker → gate → event → crop upload → event upload → merge → `open` → email → `reported` |
| Claim | email link → `/fixed/[token]` → `POST /v1/fixed/{token}` → `claimed_fixed` |
| Verify | drive → pass samples → `POST /v1/passes` → coverage → verification → `verified` or `unknown` |
| Reopen | later event within radius with `captured_at` after the claim → `reopened` → "reopened" email → `reported` |
| Offline | everything queued in the outbox; crop first, then event, then pass; uploads are idempotent |

## 5. Technology choices and fallbacks

| Part | Choice | Why | Fallback |
|---|---|---|---|
| Detector | YOLO n or s | Easy to train and export | RT-DETR or RF-DETR variant if the licence is a problem |
| Mobile runtime | LiteRT, GPU delegate | Good phone performance | ONNX Runtime Mobile or NCNN |
| Android | Kotlin native | Reliable background camera, GPS, outbox | Laptop in the car + phone for GPS, same core |
| Backend | FastAPI | Quick to write | none needed |
| DB | Postgres + PostGIS | `ST_DWithin` merge and path queries | Plain Postgres with haversine in SQL |
| Storage | MinIO / R2 / Supabase | S3 API | Local filesystem for the demo |
| Email | Resend | Simple API | `console` provider (prints the email) |
| Web | Next.js + MapLibre | Fast map UI | Leaflet |
| Edge demo | Raspberry Pi 5 (+ Hailo) | Portability proof | Pi CPU or laptop; optional |

## 6. Metric definitions (device and test must match)

All computed on the ROI (`detection.roi`) of the frame:
- **sharpness**: grayscale, resize ROI to 320 px wide (keep aspect), variance of the Laplacian (3×3 kernel). Higher is sharper. Calibrate `pass_quality.min_sharpness` on your own good and blurred footage.
- **mean_luma**: mean of the grayscale ROI, 0 to 255.
- **occlusion_ratio**: fraction of the ROI area covered by boxes of class `vehicle` (union, not sum), evaluated at up to 2 Hz and held between evaluations. If `occlusion_check_enabled` is false it is null and the UI says occlusion was not checked.
- **detector_active / inference_fps**: true only if the detector processed frames during the sample interval; `inference_fps` is the measured rate over the last second.

These definitions are the contract for the golden fixtures in `core/tests/golden/` (see `docs/TEST_PLAN.md`). The Kotlin port must reproduce them within the tolerances stated there.

## 7. Security and privacy model

- Each device has an Ed25519 key pair generated on first run; the public key is registered once.
- Requests are signed (headers) and event/pass payloads are signed (envelopes). Details in `openapi.yaml`.
- Fixed tokens: `base64url(HMAC_SHA256(FIXED_TOKEN_SECRET, "{pothole_id}:{fixed_token_epoch}"))`, 256 bits, unguessable without the secret. Only `sha256_hex(token)` is stored for lookup. The epoch is incremented when a pothole is reopened and re-notified, so the old link returns 410 while reminder emails can safely reuse the current link.
- Crops are served through short-lived presigned GET URLs.
- Admin endpoints exist only in demo mode and require `ADMIN_SECRET`.
- Retention: crops kept for a configured period after verification; raw pass samples may be trimmed after evaluation; wide frames are never stored. Exact periods are an open question (`docs/OPEN_QUESTIONS.md`).
- Threats considered: forged events (signatures), replayed requests (timestamp window), duplicate uploads (idempotent ids), token guessing (random 256-bit tokens), email abuse (test inbox only, one initial email per pothole). Out of scope for the MVP: sybil devices, GPS spoofing, coordinated false reopen attacks (note for production: require `reopen_min_events` ≥ 2 from independent devices).

## 8. Observability

Structured logs with these decision events: `signature_rejected`, `event_accepted`, `event_duplicate`, `event_merged`, `pothole_created`, `email_queued`, `email_sent`, `email_failed`, `claim_received`, `coverage_computed`, `verification_decision`, `status_changed`, `escalation_sent`. Each carries ids and, for decisions, the evidence and threshold values used. `/healthz` reports database, storage and the clock offset.

## 9. Failure modes and expected behaviour

| Failure | Expected behaviour |
|---|---|
| No network | Outbox keeps everything; uploads resume in order (crop → event → pass) |
| Crop upload succeeded but event upload failed | Retry the event; the crop key is stable |
| Event arrives before its pass (or the reverse) | Verification recomputes from stored data, so order does not matter |
| Email provider down | Report stays `pending`/`failed`, retried with backoff; pothole stays `open` until a send succeeds |
| Phone overheats | Reduce inference rate, then pause; passes mark `detector_active=false` or low `inference_fps`, so they cannot verify anything |
| Poor GPS | `accuracy_m` above the limit makes the pass unknown for affected potholes |
| Device clock wrong | Request timestamp check fails with a clear error; the app shows "check phone time" |
| Duplicate upload | Returns `duplicate`, treated as success |

## 10. Scaling notes (not for the MVP)

Spatial queries are indexed (GiST). A second worker process can take over the scheduler if needed. Pass samples as JSONB are fine for a hackathon; for a fleet, store them in a time-series or columnar table and precompute segments. Map matching (Valhalla) and H3 cells are the first upgrades for location accuracy and coverage queries.

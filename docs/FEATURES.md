# FEATURES

Status: v0.1. One section per feature. Each lists purpose, requirement IDs (from `docs/PRD.md`), priority, behaviour, edge cases and acceptance tests. Test IDs refer to `docs/TEST_PLAN.md`. Flows are in `docs/WORKFLOWS.md`; rules for statuses in `docs/STATE_MACHINE.md`.

Priority: P0 must for MVP · P1 should · P2 wow · P3 later.

---

## F-01 Detection and tracking (P0) — DET-01..05

**Purpose:** find potholes in live or recorded frames and suppress noise.
**Inputs:** frames, `detection.*` and `tracking.*` thresholds.
**Outputs:** track records (id, frames, confidences, best view).
**Behaviour:**
- Inference at `detection.target_fps` on the ROI; the same core code runs on a laptop (replay) and as a Kotlin port on the phone.
- A candidate becomes an event only after `tracking.min_track_frames` frames and mean confidence ≥ `tracking.min_mean_confidence`.
- Classes: `pothole` (events) and `vehicle` (occlusion only).
**Edge cases:** shadows, manhole covers, speed breakers, patched asphalt, wet reflections (hard negatives, see `docs/DATA_AND_MODEL.md`); one pothole seen in two separate tracks (the backend merge handles it); a track interrupted by a vehicle (use `lost_track_buffer_frames`).
**Acceptance:** `TC-DET-01` (golden fixtures: a single-frame detection yields no event; a 3-frame track yields exactly one event); `TC-DET-02` (the Kotlin port reproduces the golden outputs).

## F-02 Event creation and signing (P0) — EVT-01..04

**Purpose:** package one sighting as a verifiable record.
**Behaviour:** payload per `EventPayload` in `openapi.yaml`; road-only crop; payload serialised once to a string and signed with Ed25519; `event.id` generated on the device; severity is only a proposal (the server recomputes).
**Edge cases:** missing GPS → no event; crop larger than 1 MB → reduce JPEG quality until it fits; clock wrong → the request timestamp check will fail loudly.
**Acceptance:** `TC-ING-01` (altered payload rejected with `invalid_signature`), `TC-PRIV-01` (no object in storage is larger than `event.crop_max_side_px` or contains a wide frame; check dimensions).

## F-03 Pass logging and quality sampling (P0) — VER-01

**Purpose:** record where the camera went and whether it could really see the road, so absence of detections can be interpreted.
**Behaviour:** one pass per drive, samples every `pass_quality.sample_interval_s` with the fields of `PassSample`; metrics defined in `docs/ARCHITECTURE.md` §6; uploaded even when no pothole was found.
**Edge cases:** a throttled or paused detector must record `detector_active=false` or low `inference_fps` (never claim it ran); GPS dropouts keep the last fix with its (growing) `accuracy_m`.
**Acceptance:** `TC-DET-03` (metric functions match golden values), `TC-PASS-01` (a pass with a paused detector can never verify).

## F-04 Offline outbox and sync (P0 basic upload, P1 offline demo) — OFF-01..04

**Behaviour:** durable queue; order crop → event → pass; exponential backoff with jitter; idempotent by id; survives app restarts.
**Acceptance:** `TC-OFF-01` (5 events captured in airplane mode upload exactly once each), `TC-OFF-02` (kill the app mid-upload; no loss, no duplicates), `TC-OFF-03` (retry after 5xx).

## F-05 Device auth and ingest (P0) — API-02, EVT-03

**Behaviour:** request signature (headers) + payload signature; timestamp tolerance `auth.request_signature_tolerance_s` against the real clock; payload `device_id` must equal the header; per-event results.
**Edge cases:** unknown device (401 `unknown_device`); bad timestamp (401 `timestamp_out_of_range`); body too large (413).
**Acceptance:** `TC-ING-01..06`.

## F-06 Merge and severity (P0 merge, P0 severity bands) — MRG-01..03, EVT-04

**Merge:** nearest pothole within `merge.radius_m` (PostGIS `ST_DWithin`, ordered by distance); otherwise a new pothole. Canonical location = confidence-weighted mean of its events.
**Severity:** `score = w_area·min(area_ratio / area_ratio_saturation, 1) + w_conf·confidence + w_repeat·min(event_count / repeat_saturation, 1)`; bands by `band_medium_min` and `band_high_min`. Always described as graded, never measured.
**Edge cases:** two potholes closer than the radius merge (known limitation; documented in the UI help text); GPS error larger than the radius creates duplicates (measure it; E5).
**Acceptance:** `TC-MRG-01..04`, `TC-SEV-01..02`.

## F-07 Authority email (P0) — RPT-01..04

**Behaviour:** one `initial` email per pothole to the test inbox; subject: `[CivicLens] <severity> pothole near <place or lat,lon>`; body: photo, severity band, map link, Fixed link, short plain-language note that this is a pilot (test inbox); `reopened`, `reminder` and `escalation` variants; every send recorded in `reports`.
**Edge cases:** provider failure → retry with backoff; ten events merged into one pothole produce one email.
**Acceptance:** `TC-RPT-01..04`.

## F-08 Fixed claim (P0) — VER-04, WEB-03, API-03

**Behaviour:** rules in `docs/STATE_MACHINE.md` §3; page shows the pothole and one button; confirmation text states that a claim is not proof.
**Acceptance:** `TC-VER-18` (idempotent), `TC-VER-19` (superseded token → 410), `TC-API-05`.

## F-09 Pass coverage (P0) — VER-02, VER-03

**Behaviour:** `logic/coverage.py` implements STATE_MACHINE §4.3 as a pure function; results cached in `pass_coverage`.
**Acceptance:** `TC-COV-01..14` (baseline clean pass plus blurred, dark, occluded, inactive detector, low FPS, poor GPS, too fast, wrong heading, lateral offset, insufficient samples, boundary fractions and occlusion-check flag cases).

## F-10 Verification and reopen (P0) — VER-05..08

**Behaviour:** `logic/verification.evaluate` per STATE_MACHINE §4; applies decisions with history rows and evidence; "reopened" email with before/after.
**Edge cases:** out-of-order uploads, late events, repeated evaluation, loops crossing the same pothole twice.
**Acceptance:** `TC-VER-01..19` (all scenarios S1..S16 are encoded as tests).

## F-11 Escalation (P1) — ESC-01..03

**Behaviour:** per W-11; `reminder` after `escalation.reminder_after_days`, `escalation` after `escalation_after_days`; at most one of each per cycle.
**Acceptance:** `TC-ESC-01..03` using `simulate-time`.

## F-12 Public map (P0) — WEB-01

**Behaviour:** MapLibre map of potholes via `GET /v1/potholes`; pins coloured **and** iconed by status; legend; filter by status; click opens a popup with thumbnail, severity, status and a link to the detail page; replay and seed data carry a visible badge.
**Acceptance:** `TC-WEB-01` (all six statuses render distinctly; colour is not the only cue), `TC-WEB-02` (badge shown for replay/seed).

## F-13 Pothole detail and evidence (P0) — WEB-02

**Behaviour:** header (status, severity band, authority), timeline from `status_history` with reason texts and links to evidence, before/after photos (reopened) or the pass log (verified/unknown/claimed), coverage table (pass time, clean yes/no, reason in plain words), note "occlusion not checked" when `occlusion_check_enabled` is false, severity described as a graded band.
**Acceptance:** `TC-WEB-03` (reopened shows before and after; verified shows pass log and no fabricated "after" image).

## F-14 Impact counter (P1) — WEB-04

**Behaviour:** `/impact` and a header strip: potholes found, reports sent, claimed fixed, verified, reopened, km covered. Counts, not percentages, as the primary display.
**Acceptance:** `TC-WEB-04`.

## F-15 Android drive app (P0) — APP-01..06

**Behaviour:** three screens per `docs/SCREENS.md`; foreground service; start/stop only when stationary; consent first; only the four permission groups (camera, fine location, notifications, foreground service); model and thresholds from bundled assets.
**Edge cases:** permission denied (explain and stop); battery below 20% (warn); thermal throttling (W-02); app killed (finalize the pass on next launch).
**Acceptance:** `TC-APP-01..05` (manual checklist plus instrumented tests where practical).

## F-16 Demo tools (P0 replay, P1 skip-time and reset) — ESC-03, WEB-05

**Behaviour:** replay tool, `simulate-time`, `reset`, `/demo` page (buttons: advance 1 day / 14 days / 28 days, reset, status of services); everything labelled in the UI as demo; disabled in production.
**Acceptance:** `TC-DEMO-01..03`.

## F-17 Consent and privacy (P0) — APP-05, NFR-06

**Behaviour:** consent screen text (what is stored: road crops, GPS points, quality samples; what is not: wide frames, audio, identity); a switch to stop sharing; "delete local data" button; backend retention job removes crops after the configured period (open question).
**Acceptance:** `TC-PRIV-01..02`.

## F-18 Thermal adaptation (P1) — DET-06

**Behaviour:** per W-02 step 4. Every throttled period is visible in the pass samples.
**Acceptance:** `TC-PASS-01`, manual 20-minute run (E3).

## F-19 Raspberry Pi portability demo (P2) — DET-07

**Behaviour:** run the same `core` on a Pi 5 (CPU, or Hailo if the toolchain works) using a camera or a recorded clip; report FPS honestly. Not on the critical path.
**Acceptance:** `TC-DEMO-04` (golden fixtures pass on the Pi).

---

## Requirement traceability (summary)

| Requirement group | Features |
|---|---|
| DET-01..07 | F-01, F-18, F-19 |
| EVT-01..04 | F-02, F-06 |
| OFF-01..04 | F-04 |
| MRG-01..03 | F-06 |
| RPT-01..04 | F-07 |
| VER-01..09 | F-03, F-08, F-09, F-10 |
| ESC-01..03 | F-11, F-16 |
| APP-01..07 | F-15, F-17 |
| WEB-01..05 | F-12, F-13, F-14, F-16 |
| API-01..04 | F-05, F-08 |
| NFR-01..12 | F-01, F-03, F-04, F-05, F-17 and `docs/TEST_PLAN.md` |

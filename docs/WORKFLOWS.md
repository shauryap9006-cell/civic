# WORKFLOWS

Status: v0.1. Step-by-step flows. Each flow lists actors, trigger, steps, failure handling and the requirement IDs it satisfies. Statuses and transition rules are defined only in `docs/STATE_MACHINE.md`; field names in `openapi.yaml`.

Legend: **D** = device, **B** = backend, **A** = authority, **W** = website, **S** = storage.

---

## W-01 Device registration (first run)

Trigger: first app launch (or first run of the replay tool). Requirements: API-02, EVT-03.

1. D shows the consent screen (APP-05). Without consent, stop.
2. D generates an Ed25519 key pair. The private key is stored wrapped by an Android Keystore AES key (or an encrypted file on Linux). The public key is exported as base64.
3. D calls `POST /v1/devices/register` with `public_key`, `platform`, `app_version`, `model_version`.
4. B stores the device (if the key already exists, returns the existing `device_id`) and returns `device_id` and `server_time`.
5. D stores `device_id` and computes a clock skew estimate (`server_time` minus local time) for diagnostics only.

Failure: no network → retry with backoff; the app can detect and queue locally before registration, but nothing uploads until registered.

---

## W-02 Drive session lifecycle

Trigger: the person taps Start while stationary. Requirements: APP-01..04, VER-01, DET-06.

1. D creates `pass_id` (UUID) and starts the foreground service (persistent notification).
2. D starts camera analysis, GPS updates and the quality sampler.
3. Every frame: detector → tracker → gate (W-03). Every `pass_quality.sample_interval_s`: the quality sampler writes one sample (position, heading, speed, accuracy, sharpness, occlusion, luma, `detector_active`, `inference_fps`) into the pass buffer.
4. Thermal manager: at `thermal.reduce_at_status` lower the inference rate (never below `detection.min_fps`); at `thermal.stop_at_status` pause detection and show a message when stationary. During the reduced or paused period samples keep being recorded with the real `detector_active` and `inference_fps`, so those stretches cannot verify anything.
5. The screen dims to a minimal status view after the vehicle starts moving; Start/Stop are enabled only when `speed < 5 km/h`.
6. On Stop (or when the service is killed and restarted), D finalizes the pass: builds the pass payload, signs it, stores it in the outbox. A crashed session is finalized on next launch with `ended_at` = last sample time.
7. D shows the trip summary (potholes found, queued/sent).

Failure: phone call or app switch must not stop the foreground service; low battery → warn before start.

---

## W-03 From frame to event (on device)

Requirements: DET-01..05, EVT-01..04.

1. Frame → resize to `detection.model_input_px`, crop to `detection.roi`.
2. Detector returns boxes with class and confidence; drop boxes below `detection.detector_conf_min`. Class `vehicle` boxes feed the occlusion estimate only.
3. Tracker assigns ids. Each track accumulates frames, confidences and the best view (`confidence × area`).
4. Gate: a track becomes an event when it has ≥ `tracking.min_track_frames` frames and mean confidence ≥ `tracking.min_mean_confidence`. Emit once per track id.
5. Event builder: crop the best frame's box with `event.crop_padding_ratio` padding, limit to `event.crop_max_side_px`, JPEG at `event.jpeg_quality`, compute SHA-256. **Never** save the full frame.
6. Attach the GPS fix nearest to the best frame time (position, accuracy, heading, speed).
7. Assemble the payload JSON (see `EventPayload`), serialise it once to a string, sign that exact string, and write crop + envelope to the outbox. `event.id` = new UUID.

Failure: GPS unavailable → keep the event but mark `gps_accuracy_m` large; the backend's merge still works but verification will not trust it. No GPS at all → do not create an event (a pothole without a location is useless).

---

## W-04 Outbox upload (offline-first)

Requirements: OFF-01..04.

Order of operations for each queued item, always in this order:
1. **Crop:** `POST /v1/uploads/sign` → HTTP PUT the JPEG to `upload_url`. Mark crop `sent`.
2. **Event:** include the batch (≤ `offline.max_batch_events`) in `POST /v1/events`. For each result: `accepted` or `duplicate` → `sent`; `rejected` → `failed` with the reason (do not retry `invalid_signature` or `malformed_payload`; retry `crop_missing` after re-uploading the crop).
3. **Pass:** `POST /v1/passes` (gzip allowed). `accepted` or `duplicate` → `sent`.
4. Network or 5xx error → exponential backoff `min(base × 2^n, max)` with jitter; `429` honours `Retry-After`; `401` with a timestamp error → show "check phone time".

Passes upload after the events from the same drive when possible, but the backend does not depend on the order (W-09 recomputes).

Acceptance: 5 events captured offline appear exactly once each after reconnection (`TC-OFF-01`).

---

## W-05 Ingest, merge and first report (backend)

Requirements: MRG-01..03, EVT-03, EVT-04, RPT-01..04.

For each envelope in `POST /v1/events`:
1. Verify the request signature (headers) once per request; verify the event signature per envelope against the registered public key of `payload.device_id`; the payload `device_id` must equal `X-Device-Id` (`device_mismatch`).
2. Parse and validate the payload (`malformed_payload`, `invalid_coordinates`).
3. If `events.id` exists → `duplicate`.
4. Check that the object `crop_key` exists in storage and that its SHA-256 equals `crop_sha256` (`crop_missing`, `crop_hash_mismatch`).
5. In one transaction: find the nearest pothole within `merge.radius_m` using `ST_DWithin` (lock the row `FOR UPDATE`); if none, create a pothole (`open`, T1) with the authority resolved from location (one-city config for the MVP).
6. Insert the event, set `pothole_id`, update `event_count`, `last_event_at`, recompute the canonical location (confidence-weighted mean of its events) and severity (`logic/severity.py`).
7. Call `evaluate(pothole)`; if it returns `reopened`, apply T7 (queue the "reopened" email).
8. If the pothole is `open` and has no `initial` report: insert a `pending` report row (kind `initial`). Exactly one per pothole, guarded by a uniqueness check inside the transaction.
9. After commit, the mailer sends pending reports (W-06).

Result per event: `accepted` (with `pothole_id` and current status), `duplicate`, or `rejected` (with a reason code).

---

## W-06 Authority email

Requirements: RPT-01..04.

1. For each `pending` report: build the email — subject with location and severity band, a plain-language description, the crop image (inline link via presigned URL), a map link to `PUBLIC_BASE_URL/pothole/{id}`, and a **Fixed** link `PUBLIC_BASE_URL/fixed/{token}`.
2. The Fixed token is derived, not stored: `token = base64url(HMAC_SHA256(FIXED_TOKEN_SECRET, "{pothole_id}:{fixed_token_epoch}"))` (43 characters). The database stores only `fixed_token_hash = sha256_hex(token)` for lookup. Because the token is recomputable, `reminder` and `escalation` emails reuse the current token, so the link in the first email keeps working. A reopen (T8) increments `fixed_token_epoch`, which supersedes the old link (it then returns 410). See D-11 in `docs/DECISIONS.md`.
3. Recipient: `TEST_INBOX` when `EMAIL_MODE=test`. Record the actual `to_address`.
4. Send through the provider. On success: `status=sent`, `sent_at`, `provider_message_id`; apply T2 (initial) or T8 (reopened) — set `reported_at`/`last_reported_at`. On failure: `attempts++`, `error`, retry with backoff up to `email.max_retries`; the pothole stays `open` (initial) until a send succeeds.
5. Idempotency: a retry must not create a second `initial` row; sending is guarded by `status = pending` and `attempts` bookkeeping.

---

## W-07 Authority claims "Fixed"

Requirements: VER-04, WEB-03, API-03.

1. A opens `PUBLIC_BASE_URL/fixed/{token}`; W calls `GET /v1/fixed/{token}` and shows the pothole (photo, location, severity, current status) and a single button.
2. A presses the button; W calls `POST /v1/fixed/{token}` with an optional note.
3. B looks up the pothole by HMAC of the token, applies the claim rules from `docs/STATE_MACHINE.md` section 3, sets `claimed_fixed_at = clock.now()`, selects `before_event_id`, writes the history row (`fixed_claim`) and immediately calls `evaluate`. Evaluation only counts passes whose `crossing_at` is after `claimed_fixed_at`, so no special handling is needed for passes uploaded around the same time.
4. W shows: "Thanks. This is now marked as claimed fixed. CivicLens will verify the repair using later drives. A claim alone is not treated as proof."

Failure: 404/410 pages explain what happened (old link, reopened case).

---

## W-08 Pass ingest and coverage

Requirements: VER-01, VER-02, VER-03.

1. Verify request and payload signatures as in W-05. Validate: ≥ 2 samples, times increasing, `ended_at ≥ started_at`.
2. If `passes.id` exists → `duplicate`.
3. Insert the pass; build `path` as a LineString from sample coordinates (drop samples with `accuracy_m` above 100 m for the geometry only; keep all samples in `samples`).
4. Candidate potholes: `ST_DWithin(path, pothole.location, window_max_m + max_lateral_offset_m + 10)` and status in (`claimed_fixed`, `unknown`, `verified`).
5. For each candidate, compute coverage (STATE_MACHINE §4.3) and upsert `pass_coverage`.
6. For each candidate, run `evaluate` (W-09) and apply the decision.
7. Return `potholes_evaluated` and the list of status changes.

---

## W-09 Verification evaluation (re-runnable)

Requirements: VER-05..08.

`services/potholes.evaluate(pothole_id, now)`:
1. Lock the pothole row.
2. Load its events, and the passes with a crossing near it after `claimed_fixed_at` (recompute coverage if missing).
3. Call the pure `evaluate(...)` (STATE_MACHINE §4).
4. If the decision changes the status: update `potholes`, insert one `status_history` row with the evidence, queue side effects (the "reopened" email for T7). If the decision equals the current state: write nothing.
5. Trigger points: after event ingest, after pass ingest, after a claim, after `simulate-time`, and on an admin "re-evaluate all" call used by tests.

---

## W-10 Reopen and re-notify

Requirements: VER-07, VER-08.

1. A late detection merges into a `claimed_fixed`, `unknown` or `verified` pothole with `captured_at > claimed_fixed_at` and confidence ≥ threshold → T7.
2. B queues a `reopened` report whose email shows the **before** image (`before_event_id`) and the **after** image(s), plus the pass log summary.
3. When that email is sent: T8 → status `reported`, new Fixed token, escalation cycle restarts.
4. W shows the pothole as reopened until the email is sent, then as reported with a visible history entry.

---

## W-11 Escalation

Requirements: ESC-01..03.

The scheduler (every `escalation.check_interval_minutes`) and `simulate-time` both run:
1. Select potholes with status `reported`.
2. For each: `age = clock.now() − last_reported_at`. If `age ≥ reminder_after_days` and no `reminder` report exists in the current cycle → queue `reminder`. If `age ≥ escalate_after_days` and no `escalation` report exists in the current cycle → queue `escalation` (to `authorities.escalation_email`, or the test inbox in test mode).
3. Record `status_history` rows with `reason` `reminder_sent` / `escalation_sent`.
"Current cycle" = reports created at or after `last_reported_at`.

---

## W-12 Demo operations

Requirements: ESC-03, WEB-05.

- **Replay as a device:** `core` replay tool registers a device with platform `replay`, runs the recorded video + GPS through detector → gate → event builder → outbox, then uploads (W-04). Several replay devices can be registered to simulate multiple vehicles.
- **Pass replay:** the replay tool also emits passes with real quality metrics computed from the video. A scripted "clean pass" fixture (`core/tests/golden` or `ml/replay_set/`) is used when no real post-repair drive exists; such passes are uploaded by a device whose platform is `replay` and are shown with a **Replay** badge.
- **Skip time:** `POST /v1/admin/simulate-time {advance_seconds}` adds to the clock offset and runs escalation and re-evaluation.
- **Reset:** `POST /v1/admin/reset` clears events, passes, potholes, reports, history; resets the offset to 0.
- Both admin endpoints are disabled unless `DEMO_MODE=true` and `APP_ENV != production`.

# TEST PLAN

Status: v0.1. Every test case has an ID. Implement tests with these names (for example `test_TC_VER_05_contradicting_event_beats_clean_passes`). P0 = must pass for the MVP; P1 = should.

## 1. Principles

1. Test the pure logic first, with no database (`logic/*`).
2. Then test the services against a real Postgres/PostGIS (`make up`), not mocks.
3. Time is injected: tests pass `now` explicitly or use a fake `Clock`.
4. Synthetic data comes from factories (section 3). Real recordings are used for evaluation (section 7), not for unit tests.
5. A test that needs a threshold value overrides it explicitly in the test, so a later tuning of `config/thresholds.yaml` does not silently change what a test means.

## 2. Test levels and commands

| Level | Where | Command |
|---|---|---|
| Unit (pure logic) | `backend/tests/logic/`, `core/tests/` | `make test-core`, `cd backend && pytest tests/logic` |
| Service/API | `backend/tests/api/` (needs DB + MinIO) | `make up && cd backend && pytest tests/api` |
| Golden fixtures (device logic) | `core/tests/golden/` (Python), `android/app/src/test/` (Kotlin) | `make test-core`, Gradle test |
| Web | `web/` | `make test-web` |
| End to end | `backend/tests/e2e/test_demo_path.py` | `pytest tests/e2e` |
| Manual | checklists below | — |

## 3. Test factories (build these first)

`backend/tests/factories.py`:

```
make_pothole(lat, lon, status="claimed_fixed", claimed_fixed_at=T0, ...)
make_event(pothole=None, lat, lon, captured_at, confidence=0.8, device_id=..., pass_id=None)
make_pass(
    pothole,                      # location to drive past
    pass_id=None,                 # default new uuid
    approach_bearing_deg=0,       # direction of travel toward the pothole (straight road)
    lateral_offset_m=1.0,         # offset of the path from the pothole
    speed_mps=10.0,
    start_distance_m=60.0,        # where the pass starts before the pothole
    end_distance_m=20.0,          # how far past the pothole it continues
    crossing_time=T0 + 1 day,     # time of closest approach
    hz=5,                         # sample rate (matches sample_interval_s)
    quality=dict(sharpness=120, occlusion_ratio=0.0, mean_luma=120, accuracy_m=5,
                 detector_active=True, inference_fps=10),
    degrade=lambda i, d: {},      # per-sample override hook: i = index, d = distance to pothole
)
```
The generator places samples along a straight line using flat-earth offsets that are accurate to well under a metre at these distances; the assertions in the tests rely on `logic/geo.py` for distance, so the two are tested against each other (`TC-GEO-01`).

Scenario helper: `scenario(...)` composes events and passes with times for S1..S16 so each scenario test reads like the table in `docs/STATE_MACHINE.md`.

## 4. Test cases

### 4.1 Geometry (P0)
| ID | Case | Expected |
|---|---|---|
| TC-GEO-01 | haversine and bearing against known coordinate pairs; factory distances agree with `geo.py` within 0.5 m | pass |
| TC-GEO-02 | heading difference wraps correctly (350° vs 10° = 20°) | pass |

### 4.2 Detection and quality metrics (device logic; golden fixtures)
| ID | Case | Expected | Pri |
|---|---|---|---|
| TC-DET-01 | Track of 1 frame → no event; 3 frames with mean confidence ≥ threshold → exactly one event; 3 frames below mean confidence → none | per golden file `gate_cases.json` | P0 |
| TC-DET-02 | Kotlin port reproduces the golden gate/tracker outputs | identical event list | P0 |
| TC-DET-03 | Metric functions (`sharpness`, `mean_luma`, `occlusion_ratio`) on golden images match expected values within tolerance (sharpness ±5%, luma ±2, occlusion ±0.03) | pass | P0 |

Golden fixtures: `core/tests/golden/gate_cases.json` (sequence of per-frame detections → expected events) and `core/tests/golden/metrics/` (small images + `expected.json`). Create at least: sharp road, motion-blurred road, dark road, road with a vehicle covering ~40% of the ROI.

### 4.3 Ingest and auth (P0)
| ID | Case | Expected |
|---|---|---|
| TC-ING-01 | Event payload altered after signing | result `rejected`, reason `invalid_signature`; nothing stored |
| TC-ING-02 | Request timestamp 10 minutes old (tolerance 300 s) | 401 `timestamp_out_of_range` |
| TC-ING-03 | Same event id uploaded twice | second result `duplicate`; one row in `events` |
| TC-ING-04 | Payload `device_id` differs from `X-Device-Id` | `rejected`, `device_mismatch` |
| TC-ING-05 | `crop_key` not in storage / wrong SHA-256 | `crop_missing` / `crop_hash_mismatch` |
| TC-ING-06 | Unregistered device | 401 `unknown_device` |
| TC-ING-07 (P1) | Body above the limit | 413 |

### 4.4 Merge and severity (P0)
| ID | Case | Expected |
|---|---|---|
| TC-MRG-01 | Event 4 m from an existing pothole | merged; `event_count` = 2 |
| TC-MRG-02 | Event 14 m away (radius 10) | new pothole |
| TC-MRG-03 | Event equidistant between two potholes | joins the nearest (deterministic tie-break by oldest id) |
| TC-MRG-04 | Location after merge equals the confidence-weighted mean (within 0.01 m) | pass |
| TC-SEV-01 | Large crop, high confidence, many repeats → `high`; small, low confidence, one event → `low` | pass |
| TC-SEV-02 | Band boundaries at exactly `band_medium_min` / `band_high_min` | upper band (≥) |

### 4.5 Email (P0)
| ID | Case | Expected |
|---|---|---|
| TC-RPT-01 | Ten events merged into one pothole | one `initial` report, one email |
| TC-RPT-02 | `EMAIL_MODE=test` | `to_address` equals `TEST_INBOX` even though the authority has another address |
| TC-RPT-03 | Provider failure then success | `attempts` increments, a single email is delivered, status `reported` only after success |
| TC-RPT-04 | Email content | contains crop link, severity band, map link, Fixed link; contains no coordinates beyond the map link text and no depth/size claims |

### 4.6 Coverage (pure; P0)
Each case uses `make_pass` with `degrade` as noted; `required window samples` etc. come from thresholds.
| ID | Case | Expected `clean` / `reason` |
|---|---|---|
| TC-COV-01 | Straight pass, lateral offset 1 m, all quality good | clean / `ok` |
| TC-COV-02 | Sharpness below `min_sharpness` for the whole window | not clean / `blurred` |
| TC-COV-03 | `mean_luma` below `min_mean_luma` | not clean / `too_dark` |
| TC-COV-04 | `occlusion_ratio` 0.5 in the window | not clean / `occluded` |
| TC-COV-05 | `detector_active=false` in the window | not clean / `detector_inactive` |
| TC-COV-06 | `inference_fps` 2 | not clean / `low_inference_fps` |
| TC-COV-07 | `accuracy_m` 40 | not clean / `poor_gps` |
| TC-COV-08 | `speed_mps` 30 | not clean / `too_fast` |
| TC-COV-09 | Heading 120° off the bearing to the pothole | not clean / `insufficient_window_samples` |
| TC-COV-10 | Lateral offset 12 m | no coverage row (no crossing) |
| TC-COV-11 | Very fast short window giving fewer than `min_window_samples` samples | not clean / `insufficient_window_samples` |
| TC-COV-12 | 70% of window samples OK (threshold 0.80) → not clean; 85% → clean | boundary behaviour |
| TC-COV-13 | `occlusion_check_enabled=false`, `occlusion_ratio=null` | clean allowed; flag returned for the UI note |
| TC-COV-14 | `occlusion_check_enabled=true`, `occlusion_ratio=null` | not clean / `occlusion_unknown` |

### 4.7 Verification scenarios (pure and service level; P0)
`TC-VER-nn` = scenario `Snn` in `docs/STATE_MACHINE.md` §6, run first as a pure test and then through the API.

| ID | Scenario | Expected final status |
|---|---|---|
| TC-VER-01 | S1 claim → 2 clean passes (different ids) | `verified` |
| TC-VER-02 | S2 claim → 1 clean pass | `claimed_fixed` |
| TC-VER-03 | S3 claim → blurred pass | `unknown` |
| TC-VER-04 | S4 blurred, clean, clean | `verified` |
| TC-VER-05 | S5 claim → clean pass → detection | `reopened` |
| TC-VER-06 | S6 verified → later detection | `reopened` |
| TC-VER-07 | S7 clean pass before the claim is ignored | `claimed_fixed` |
| TC-VER-08 | S8 late upload of a pre-claim event | unchanged |
| TC-VER-09 | S9 one pass crossing twice | counts once |
| TC-VER-10 | S10 same pass id uploaded twice | counts once |
| TC-VER-11 | S11 pass uploaded before the explaining event, and the reverse | identical outcomes |
| TC-VER-12 | S12 event from the same device inside the pass | pass not clean (`detection_in_pass`), pothole reopened |
| TC-VER-13 | S13 claim repeated | unchanged, 200 |
| TC-VER-14 | S14 old link after reopen | 410 |
| TC-VER-15 | S15 pass 12 m to the side | no coverage |
| TC-VER-16 | S16 pass facing away | not clean |
| TC-VER-17 | Evaluate the same data 5 times | same decision; exactly one history row per real change |
| TC-VER-18 | API: `POST /v1/fixed/{token}` twice | second returns 200 and the same `claimed_fixed_at` |
| TC-VER-19 | API: token after reopen | 410 `token_superseded`; the new link works |
| TC-VER-20 (P1) | Contradicting event with confidence 0.3 (threshold 0.5) | attached, no reopen |
| TC-VER-21 (P1) | `reopen_min_events=2` with one event | no reopen; with two → reopen |

### 4.8 Pass ingest and honesty (P0)
| ID | Case | Expected |
|---|---|---|
| TC-PASS-01 | Pass recorded with a paused/throttled detector over the pothole | never counts as clean |
| TC-PASS-02 | Pass with 0 detections but good quality | accepted; coverage computed; pothole evaluated |
| TC-PASS-03 | Pass signature invalid | 401/`rejected`; nothing stored |
| TC-PASS-04 | Pass with decreasing timestamps | 422 |

### 4.9 Offline and sync (P0/P1)
| ID | Case | Expected | Pri |
|---|---|---|---|
| TC-OFF-01 | 5 events created offline, network restored | each appears exactly once | P1 |
| TC-OFF-02 | App killed mid-upload and restarted | no loss, no duplicates | P0 |
| TC-OFF-03 | Backend returns 503 twice then 200 | retries with backoff; one event stored | P0 |

### 4.10 Escalation and demo tools (P1)
| ID | Case | Expected |
|---|---|---|
| TC-ESC-01 | `simulate-time` +14 days on a `reported` pothole | one `reminder` report, history `reminder_sent` |
| TC-ESC-02 | +28 days | one `escalation` report; no second reminder |
| TC-ESC-03 | Running the job twice in the same state | no duplicates |
| TC-DEMO-01 | `simulate-time` when `DEMO_MODE=false` | 404 |
| TC-DEMO-02 | `reset` clears data and resets the offset to 0 | pass |
| TC-DEMO-03 | `/demo` page buttons call the endpoints and show the result | pass |
| TC-DEMO-04 (P2) | Golden fixtures pass on the Raspberry Pi | pass |

### 4.11 API contract (P0)
| ID | Case | Expected |
|---|---|---|
| TC-API-01 | Every response of every endpoint validates against `openapi.yaml` (schemathesis or a custom validator) | pass |
| TC-API-02 | Error bodies use the `Error` schema with stable codes | pass |
| TC-API-03 | `GET /v1/potholes` bbox and status filters | correct subset |
| TC-API-04 | `GET /v1/potholes/{id}` includes `history`, `coverage_after_claim`, `before_event`, `after_events` | pass |
| TC-API-05 | `GET /v1/fixed/{token}` for each status returns the right `can_claim` and message | pass |

### 4.12 Website (P0/P1)
| ID | Case | Expected | Pri |
|---|---|---|---|
| TC-WEB-01 | Map renders all six statuses with distinct colour **and** icon/label; legend present | pass | P0 |
| TC-WEB-02 | Replay and seed data show a badge | pass | P0 |
| TC-WEB-03 | Reopened shows before and after; verified shows pass log and no "after" image | pass | P0 |
| TC-WEB-04 | Impact counters match `/v1/stats` | pass | P1 |
| TC-WEB-05 | Fixed page states: ready, already claimed, reopened/410, not found | pass | P0 |

### 4.13 Android (manual checklist plus instrumented tests where practical)
| ID | Case | Pri |
|---|---|---|
| TC-APP-01 | Start is disabled while moving; enabled when stationary | P0 |
| TC-APP-02 | Foreground service continues with screen off and across an incoming call | P0 |
| TC-APP-03 | App killed during a drive: next launch finalises the pass and uploads it | P0 |
| TC-APP-04 | Consent required before the camera starts; denying permissions shows an explanation | P0 |
| TC-APP-05 | 20-minute run: record FPS, temperature, battery (E3) | P1 |

### 4.14 Privacy (P0)
| ID | Case | Expected |
|---|---|---|
| TC-PRIV-01 | Inspect uploaded objects | only crops, longest side ≤ `crop_max_side_px`; no wide frame |
| TC-PRIV-02 | Inspect 50 random crops of real drives | no readable plates or faces (record the result; if any, tighten the crop or add blurring before any pilot) |

## 5. End-to-end test (P0)

`test_demo_path`: reset → register replay device → upload crop+event → pothole `open`→`reported` (console email captured, token extracted) → `POST /v1/fixed/{token}` → upload two clean passes from different ids → `verified` → upload an event after the claim → `reopened` → "reopened" email → `reported`. Assert the full `status_history` sequence and reason codes, and that exactly two emails (`initial`, `reopened`) exist.

## 6. Continuous checks

- `make lint` and `make test` on every change.
- Contract check: a CI step validates `openapi.yaml` (OpenAPI 3.0.3) and that `schema.sql` applies to a fresh Postgres/PostGIS container.
- Threshold check: a test fails if any numeric literal from `config/thresholds.yaml` appears hard-coded in `backend/app` or `core/civiclens_core` (simple grep test).

## 7. Evaluation protocol (honest numbers for the submission)

All results go in `ml/results/<date>/report.md` with counts and the test-set description. Mark every simulated case. Never present a replay as a real repair.

| ID | Experiment | Procedure | Report |
|---|---|---|---|
| E1 | Detector quality | Route-split test set (routes not used in training); run the exported model at the deployment threshold; match boxes with IoU ≥ 0.5 | Precision, recall, PR curve; the earlier report's target was about 0.85 recall at 0.70 precision [To verify, may be revised] |
| E2 | False alarms | Drive or replay stretches with no potholes (minimum 5 km) through the whole pipeline (gate included) | Events per km; list and show every false event |
| E3 | Phone performance | Mounted phone, real drive or a looped clip, 15–20 minutes, charger connected, record FPS each minute, thermal status, battery change | Sustained FPS (median, 5th percentile), time to first throttle, temperature |
| E4 | Verification quality | Replay set (T-60): for each unfixed pothole, passes of each quality type; for each repaired pothole, clean passes. Compare **gated** verification with **naive absence** (a pothole is verified when no detection occurs in N passes, no quality gating) | Table of counts: verified correctly, falsely verified (false closures), unknown; naive vs gated; label each case real or simulated |
| E5 | Location error | Mark 15–20 potholes with a ground-truth point (photo + map pin or a careful GPS reading) and compare with the canonical pothole location | Median and 90th percentile error (m); suggested merge radius |
| E6 | Offline | Airplane-mode test of TC-OFF-01 | Pass/fail and counts |

Reporting rules: counts with percentages (for example "0 false closures in 6 unfixed cases"), the size of every test set, the date, the model version, the thresholds file hash, and a note on what was real versus simulated.

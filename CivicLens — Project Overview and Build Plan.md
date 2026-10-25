# CivicLens — Project Overview and Build Plan

Draft v1 · 25 October 2026 · Living document. Update the decision log (section 18) whenever a choice changes.

## 0. How to read this document

- Every claim carries one of four tags: **\[Measured\]** we ran it and have numbers; **\[Assumed\]** a design choice or estimate that is not yet tested; **\[Simulated\]** shown in the demo through replay or a mock; **\[To verify\]** comes from a third party or from memory and must be checked before it goes on a slide.
- Numbers in the algorithm sections are starting values to tune, not results.
- Sections 8 to 10 (algorithms, data model, API) are written so three people can build in parallel without constantly asking each other questions.
- Sections 2 and 5 record the reasoning behind the scope, so every decision stays explainable to judges and teammates.

## 1. Overview

### 1.1 One-line pitch

CivicLens turns an Android phone (later, any Android or Linux camera) into a pothole reporter that does not stop at reporting: it checks later whether the hole was really fixed, and reopens the case if it was not.

### 1.2 Problem

- Potholes damage vehicles, cause accidents and waste fuel. Complaint channels exist, but tickets get marked resolved without anyone confirming the road was repaired.
- Existing apps mostly detect and report. The gap is closing the loop: was it actually repaired?
- **\[To verify\]** Add one or two sourced statistics here (road accidents linked to potholes, share of complaints closed without repair) and link the sources.

### 1.3 What it does in five sentences

1. The phone, mounted as a dashcam, runs a detector on the road ahead while the person drives.
2. A pothole becomes an event only after it is seen in several consecutive frames; then a road-only crop, GPS point and time are saved and signed.
3. The backend merges nearby events into one pothole and emails the responsible authority once, with photo, map link and a severity band.
4. The authority can click a Fixed link. This is only a claim.
5. Later drives over the same road produce quality-checked clean passes. Enough clean passes verify the repair, and any new detection reopens the pothole with a before/after record.

### 1.4 What is new

- Prior art exists. The earlier deep report names iWatchRoadv2 (which has a basic repair check) and a Pothole Reporter project. **\[To verify\]** Re-check both before they appear on a slide.
- Our addition is repair verification gated by pass quality. Not seeing a pothole is not proof of repair. It counts only when the camera could really have seen it: right distance, no blur, no occlusion, enough light.
- Wording rule: never say we are first or that nobody else verifies repairs. Say: others check repairs naively; we gate them by pass quality.

### 1.5 Goals

- A working end-to-end slice: detect, email, Fixed click, later pass, reopen with before/after.
- Honest, measured numbers: FPS on a real phone, false alarms per km, false closure rate shown as counts.
- A credible path from phone app to licensed module.

### 1.6 Non-goals (these protect the scope)

- No claim of measuring depth or volume. Severity is graded in bands.
- No emails to real authorities during the hackathon. A test inbox only.
- No iOS in the MVP.
- No installation into closed dashcams or factory car cameras.
- No model trained from scratch. No Kubernetes, Kafka, GraphQL or blockchain.

## 2. Review of the original plan

The CivicLens Edge Deep Product and Build Report was reviewed on 25 October 2026.

| Area | Score | Reason |
| --- | --- | --- |
| Idea and differentiation | 8.5/10 | Proving a road was fixed is a sharper hook than detecting potholes. |
| Honesty and judge-readiness | 9/10 | Cites prior art, has a real-vs-simulated table, anticipates questions. |
| Technical depth | 8.5/10 | The valid-clean-pass idea is the best insight in the report. |
| Hackathon feasibility as written | 5.5/10 | Too much hardware, infrastructure and pre-event preparation. |
| **Overall as written** | **7/10** |  |
| **Overall with the trimmed MVP** | **8.5/10** |  |

### 2.1 Problems found and fixes

1. **Tiers contradicted the pitch.** Repair verification was tagged Wow, yet the pitch, demo twist and differentiator all depend on it. Without it the project is just detection plus email. **Fix:** verification is a Must.
2. **Overclaim.** The phrase the part nobody else gets right conflicts with the report's own prior-art findings. **Fix:** use the wording rule in 1.4.
3. **Hardware risk.** Raspberry Pi 5, Hailo accelerator, 10 Hz GNSS and an IMU depend on shipping times and a toolchain the report itself says is unconfirmed for the newest YOLO version. **Fix:** phone first; Pi is an optional portability proof.
4. **Thin repair data.** Perhaps 15 to 20 revisited locations will yield only a few real repairs, so a percentage such as 2% is meaningless. **Fix:** always show counts (for example 3 of 4) and label replays as simulated.
5. **Over-engineered belief model.** A log-odds update with per-condition false-negative rates is elegant but not needed on stage. **Fix:** rule-based pass gating now; log-odds on the roadmap.
6. **Unchecked prior-art claims.** Not independently verified in this review. **Fix:** verify before slides.

## 3. Users and scenarios

| Actor | What they do |
| --- | --- |
| Driver (contributor) | Mounts the phone, starts a drive, sees the count of reports. Never interacts while moving. |
| Authority officer | Receives one email per pothole, clicks Fixed after repair. |
| Citizen | Browses the public map and a pothole's history. |
| Fleet manager (later) | Deploys many devices and reads aggregate reports. |
| Judge | Watches the demo and checks the evidence. |

**User stories**

- As a driver, I start a drive and the phone detects potholes without my touching it.
- As a driver with no signal, I want reports saved and sent when I am online again.
- As an officer, I get one clear email with a photo, a map link and a severity band, not a flood of duplicates.
- As an officer, I click one link to say the repair is done.
- As a citizen, I can see whether a reported pothole is open, claimed fixed, verified or reopened, with evidence.
- As a judge, I can see why a repair counts as verified.

## 4. Scope and MVP

Build one vertical slice that serves the 3.5-minute demo: **detect, email, Fixed click, later pass, reopen with before/after.**

### 4.1 Scope tiers

**Must (about 60% of the time)**

- Detector fine-tuned on public data plus own footage, with tracking and a 3-or-more-frame gate (YOLOv8n or s is the safe pick).
- Runs on whatever device is reliable (phone, laptop, Pi CPU). Report FPS honestly.
- GPS tag, road-only crop, signed event.
- FastAPI backend with Postgres. Simple distance-based duplicate merge. No H3, no map matching.
- One email per pothole to a test inbox: photo, map link, severity band.
- Map with status colours.
- Verification: pass logging, valid-clean-pass checklist, Fixed magic link, reopen on later detection, before/after screen with the pass log.

**Should (if hours remain)**

- Offline outbox in SQLite (about 30 lines, strong demo moment: unplug the internet).
- One-city authority configuration in JSON.
- A demo-only skip 14 days button to show the escalation ladder.

**Wow (only after everything above works)**

- Pi 5 with Hailo as the demo target (portability proof, not the foundation).
- IMU confirmation.
- Two or three evaluation charts, best one: gated verification versus naive absence.

**Cut (put on the roadmap slide)**

- Map matching (Valhalla), crop verifier model, cloud vision-language judge, portal-filing agent, contractor and defect-liability link, contributor leaderboard app, retraining and OTA, multi-vehicle consensus (simulate with replayed clips), depth estimation.

### 4.2 Demo script (about 3.5 minutes)

| Time | Beat |
| --- | --- |
| 0:00 to 0:30 | Problem and one-line pitch. |
| 0:30 to 1:15 | Drive or replay: on-screen detection, event created, count increments. |
| 1:15 to 1:45 | Email lands in the test inbox with photo, map link, severity band. |
| 1:45 to 2:15 | Website map and pothole detail page. |
| 2:15 to 2:45 | Click Fixed: status becomes claimed fixed. Replay a clean pass: verified. |
| 2:45 to 3:05 | Replay a pass with a new detection: reopened, before/after shown. |
| 3:05 to 3:20 | Turn off the internet: reports queue, then upload once with no duplicates. |
| 3:20 to 3:30 | Numbers slide and roadmap. |

### 4.3 Real versus simulated

| Item | Status |
| --- | --- |
| Detection on recorded and live video | Real |
| Phone running the detector | Real |
| Email to a test inbox | Real |
| Authority clicking Fixed | Simulated (we click it) |
| Later clean pass after a repair | Simulated by replay unless a real repair happens |
| Escalation timers | Simulated by the skip button |
| Multiple devices | Simulated by replaying clips as separate devices |

### 4.4 Timeline note

If the event is in an idea or deck round, the full build is not needed yet. A 60-second video of detection producing an email, plus the verification concept and prior-art honesty, is a strong submission. Build the full slice if shortlisted. Record a drive with its GPS log that the code can replay; it is the demo backup and speeds development.

## 5. Product form and distribution

### 5.1 Where the software can run

| Target | Installable? | Notes |
| --- | --- | --- |
| Android phone as dashcam | Yes, a normal app | Has camera, GPS, IMU, 4G, compute. Easiest path. |
| Android-based dashcams, head units, fleet cameras | Yes, by sideloading the APK | Your real put-it-in-a-camera market. |
| Linux devices (Pi, Jetson, industrial cameras) | Yes, package or container | The edge-hardware demo. |
| Normal closed dashcams | No | Firmware is closed and signed. Some stream video over Wi-Fi or RTSP, so a phone or small box could process the stream. Varies by model. |
| Factory car cameras | No | OEM partnerships only, years away. |

Honest pitch: a portable module for any Android or Linux camera, shipped as an app or SDK, with a path to dashcam makers.

### 5.2 Portable design

- **Lens core (identical everywhere):** detector, tracker, gate, event builder, signing, outbox. Export the model once to ONNX or TFLite.
- **Thin wrappers:** an Android app (camera, GPS and IMU APIs, TFLite or NCNN), a Linux service (libcamera, gpsd, HailoRT or ONNX Runtime), later a vendor SDK.
- **Companion app:** controls the device, not the detection. It pairs by QR, toggles privacy settings, shows trip history and the impact counter, and has a manual report button. The device keeps detecting and queueing when the app is closed.

### 5.3 Phone-first decision

The Android app is built first, with the phone as the camera. This removes hardware risk. The same model then runs on a Pi as a portability proof if hardware arrives early.

- Android is much easier than iOS, because iOS limits background camera use. Say Android first.
- Phones overheat and throttle on a hot windshield. Test sustained FPS for 15 to 20 minutes.
- Phone GPS is rougher than a 10 Hz u-blox module, so duplicate merge and location accuracy are weaker. Report the measured error and name the better GNSS module as the product upgrade.

### 5.4 Selling to dashcam companies

**Why they might buy:** a differentiating feature, a new revenue line (subscription or data service for fleets and authorities), and lower cost than building and tuning a detector themselves.

**Obstacles**

1. Chip compatibility: each vendor has its own toolchain and often a weak or absent AI accelerator, so conversion, quantization and tuning is real work per partner.
2. Thin margins on budget dashcams. Better early buyers: fleet-camera and Android or premium dashcam makers.
3. Licensing: Ultralytics YOLO is AGPL-3.0. Selling it inside closed firmware needs their enterprise licence or a permissively licensed detector. **\[To verify\]** Decide early.
4. Long sales cycles. Companies want a pilot with measured results.
5. The cloud loop is part of the value, so you sell a service and must run and support a backend. That also gives recurring revenue.

**Order of steps:** win the hackathon; pilot with a small fleet (cabs, delivery vehicles, school buses); pitch fleet and Android-dashcam companies with real numbers; only then approach larger makers or OEMs. Talk to a few fleet managers or dashcam resellers early and ask what they would pay for and which chips they use.

## 6. System architecture

```
Android app (camera, detector, tracker, gate, signer, SQLite outbox)
      |  HTTPS, signed events and pass logs
      v
FastAPI backend  <---->  Postgres + PostGIS   +   Object storage (crops)
      |
      +-- workers: merge, email, verification, escalation
      |        |
      |        +--> Resend (test inbox)
      v
Website (Next.js + MapLibre) reads the public API
Authority opens the Fixed magic link -> backend
```

**On the device:** detection, tracking, gating, crop, signing, queueing, retry. **On the server:** merging, severity, emailing, pass coverage, verification, escalation, public API.

**Main path:** frame, detector, tracker, gate (3 or more frames), event (crop, GPS, time, signature), queue, upload, merge into pothole, email, status history.

**Offline path:** events and pass logs wait in the local queue. On reconnection they upload in order, using the event id as an idempotency key so retries never create duplicates.

## 7. Tech stack

| Part | Pick | Why or fallback |
| --- | --- | --- |
| Detector | Ultralytics YOLO, n or s size, fine-tuned | Easiest to train and export. AGPL: check licence, or use a permissive detector (RT-DETR or RF-DETR variants, verify the licence of the size you pick). |
| Labelling and tracking runs | Roboflow, Weights and Biases | Saves days of setup. |
| Tracking | ByteTrack or BoT-SORT | Lightweight, enough for the gate. |
| On-device runtime | LiteRT (formerly TFLite) with GPU delegate, or ONNX Runtime Mobile | Export INT8 or FP16. Measure sustained FPS. |
| Android app | Kotlin, CameraX, foreground service, Room | Reliable background camera, GPS and offline queue. Flutter and React Native make continuous inference harder. |
| Edge demo (optional) | Raspberry Pi 5 with Hailo AI HAT+ | Same code, different device. Only if hardware arrives early. |
| Backend | FastAPI, Pydantic, async SQLAlchemy | Fast to write, good for ML-adjacent code. |
| Database | Postgres with PostGIS on Supabase or Neon | Proper within-10-m queries. |
| Spatial grouping | H3 (optional) | For grouping road stretches and passes. |
| Jobs | ARQ or a cron-style worker | No heavy queue. |
| Photo storage | Cloudflare R2 or Supabase Storage | Cheap, S3-compatible. |
| Email | Resend | Simple API. Test inbox only. |
| Website | Next.js, Tailwind, shadcn/ui, MapLibre GL | Free OSM-based tiles. |
| Device trust | Ed25519 signatures per device | A few lines, strong tamper-evidence story. |
| Deploy | Docker, then Railway, Fly.io or Render | docker compose up locally. |
| Map matching | Valhalla or OSRM | Stretch goal only. |

**Where to be advanced:** pass-quality gating, a small evaluation set with charts, offline-first behaviour shown live, signed events with a clear privacy design, and honest FPS from a real phone.

**Avoid:** Kafka, Kubernetes, microservices, GraphQL, blockchain, training from scratch, running a large vision-language model on the phone. **\[To verify\]** Library versions and licences change; check current docs before committing.

## 8. Algorithms and logic

All values are starting points to tune **\[Assumed\]**.

### 8.1 Detection and tracking

- Input: camera frames at reduced resolution (start 640 px on the long side), region of interest on the road ahead.
- Detector outputs boxes, confidence and class. Keep a deliberately low-ish confidence for recall, then let the tracker and gate remove noise.
- Tracker assigns an id per candidate. A candidate becomes an event only if it is tracked for at least 3 frames and its mean confidence passes a threshold.
- Edge cases: shadows, manhole covers, speed breakers, wet patches and painted markings. Label these as hard negatives.

### 8.2 Event creation and signing

- Save a crop of the road region only (no wide frame, so no face or plate blurring is needed), the GPS fix at the moment of best view, heading, speed, time, confidence, track length and model version.
- Event id is generated on the device (UUID), which makes uploads idempotent.
- Sign the canonical payload with the device's Ed25519 private key. The backend verifies the signature against the registered public key.

### 8.3 Duplicate merge

- New event within 10 m of an existing open pothole (PostGIS distance query) merges into it. Otherwise it starts a new pothole.
- Canonical location: the confidence-weighted mean of event locations, recomputed on merge.
- Weakness: phone GPS error can exceed 10 m. Measure it and tune the radius; do not claim better accuracy than measured.

### 8.4 Severity banding

- Three bands (low, medium, high) from crop area relative to lane width, confidence and repeat sightings. Exact thresholds are tuned on labelled examples.
- Always described as graded, not measured. No depth claims.

### 8.5 Pass logging and quality gating

Every drive uploads pass records: the path driven and per-segment quality flags (blur, occlusion, light, speed, camera pitch). The server computes pass coverage for each known pothole.

A pass is a **valid clean pass** for pothole P only if all hold:

- The path comes within the viewing window of P: approach distance roughly 5 to 25 m.
- Image sharpness above a threshold (not blurred by speed or vibration).
- No occlusion flag (no truck, bus or vehicle blocking the road ahead).
- Adequate light (not deep night or direct glare).
- GPS accuracy good enough to say the camera was on that road.

Otherwise the pass is unknown for P and does not count as evidence either way.

### 8.6 Verification state machine

| From | To | Rule |
| --- | --- | --- |
| (new) | open | First event creates the pothole. |
| open | reported | Email sent to the authority. |
| reported | claimed fixed | Authority clicks the Fixed link. A claim, not proof. |
| claimed fixed | verified | At least 2 valid clean passes after the claim with no detection (ideally from different drives). |
| claimed fixed | unknown | Passes after the claim exist but none is valid. |
| claimed fixed or verified | reopened | A confident detection within the merge radius after the claim. Before/after evidence is attached. |
| reopened | reported | Authority is notified again. |

Every transition writes a status history row with its evidence (event ids, pass ids).

### 8.7 Escalation timers

- If a reported pothole has no Fixed claim after N days (start 14), send a reminder. After a second interval, escalate to the configured escalation contact. For the demo, a protected skip-time endpoint advances the clock **\[Simulated\]**.

### 8.8 Offline queue and retries

- Local SQLite stores events and pass logs with a state (pending, sent, failed).
- Upload uses exponential backoff with jitter. The event id is the idempotency key. Crops upload through presigned URLs.

### 8.9 Roadmap upgrade: probabilistic verification

Replace the counting rule with a log-odds belief update using per-condition false-negative rates measured from the evaluation set. Not needed for the MVP.

## 9. Data model

```
devices
  id uuid PK, public_key text (Ed25519, base64), platform text,
  app_version text, model_version text, created_at, last_seen_at

authorities
  id uuid PK, name, city, email, escalation_email

events
  id uuid PK (generated on device)
  device_id FK, pothole_id FK null
  captured_at timestamptz, received_at timestamptz
  location geography(Point,4326), gps_accuracy_m, heading_deg, speed_mps
  confidence real, track_frames int, severity_band text
  crop_url text, signature text, model_version text

potholes
  id uuid PK, location geography(Point,4326), status text
  severity_band text, event_count int, authority_id FK
  first_seen_at, last_seen_at, reported_at,
  claimed_fixed_at, verified_at, reopened_at
  fixed_token_hash text

passes
  id uuid PK, device_id FK, started_at, ended_at
  path geography(LineString,4326), quality jsonb

pass_coverage
  pass_id FK, pothole_id FK, clean boolean, reason text

reports
  id uuid PK, pothole_id FK, authority_id FK
  kind text (initial, reminder, escalation)
  sent_at, provider_message_id, status

status_history
  id uuid PK, pothole_id FK, from_status, to_status,
  reason text, evidence jsonb, at timestamptz
```

**Indexes:** GiST on events.location and potholes.location; btree on potholes(status), events(pothole\_id), pass\_coverage(pothole\_id).

**Retention \[Assumed\]:** keep crops for a fixed period after a pothole is verified; trim raw GPS tracks; never store full wide frames.

## 10. API specification

Auth: devices sign request payloads with Ed25519. Public reads need no auth. Fixed links use unguessable single-purpose tokens (only a hash is stored). The simulate endpoint needs an admin secret and is disabled in production.

| Method and path | Purpose | Notes |
| --- | --- | --- |
| POST /v1/devices/register | Register a device (public key, platform, app and model version) | Returns device id. |
| POST /v1/uploads/sign | Get a presigned URL for a crop | Short-lived. |
| POST /v1/events | Upload a batch of signed events | Idempotent by event id. Returns accepted and rejected ids with reasons. |
| POST /v1/passes | Upload pass records with quality flags | Server computes pass coverage. |
| GET /v1/potholes | List for the map | Filters: bounding box, status. |
| GET /v1/potholes/{id} | Detail | Events, passes, status history, before/after. |
| GET /v1/fixed/{token} | Authority page showing the pothole | Read-only preview. |
| POST /v1/fixed/{token} | Authority claims it fixed | Sets claimed fixed. |
| GET /v1/stats | Impact counters | Reports, verified repairs, reopened. |
| POST /v1/admin/simulate-time | Advance the clock (demo only) | Admin secret required. |

**Error handling:** 4xx errors are not retried by the app except 429; 5xx and network errors use backoff. A rejected signature is logged and surfaced on the device.

## 11. Component design

### 11.1 Android app

- **Screens (three):** drive screen (preview, Start and Stop, status: GPS lock, FPS, queued reports); settings (consent, test city); trip summary (potholes found, reports sent).
- **Foreground service** with a persistent notification keeps detection running. CameraX ImageAnalysis feeds the model.
- **Permissions:** camera, fine location, notifications, foreground-service types.
- **Power and heat:** read the thermal status API and lower inference rate when throttled. Recommend charging while driving. Test for 15 to 20 minutes.
- **Driver safety:** no interaction needed while moving; large Start and Stop only when stationary.
- **Offline queue:** Room database for events and passes with retry.

### 11.2 Backend

- Modules: device auth, event ingest, merge, severity, pass coverage, verification, email, escalation, public API.
- Background jobs: email sender, verification evaluator (runs after each pass upload), escalation scheduler.
- Local setup: one docker compose up for API, Postgres and a storage emulator.

### 11.3 Website

- **Map:** MapLibre with pins coloured by status (open, reported, claimed fixed, verified, reopened) plus a legend. Do not rely on colour alone; add icons or labels.
- **Pothole detail:** timeline, before/after photos, pass log, severity band, evidence behind each status.
- **Authority Fixed page:** one button, confirmation text, no login.
- **Impact counter and a demo-only skip time control.**

## 12. Model and data

- **Class definition:** one class, pothole. Write down what counts and what does not (shadows, manhole covers, speed breakers, patches) before labelling, and keep hard negatives.
- **Datasets:** public pothole sets (Roboflow Universe, Kaggle) plus own footage. **\[To verify\]** Check each dataset licence and commercial-use terms.
- **Own footage target (from the report):** about 20 km of footage, about 500 labelled frames, and 15 to 20 pothole locations that you revisit over time. Scale this to the time you actually have.
- **Split by route, not by frame,** so near-identical frames do not leak between training and test.
- **Training:** fine-tune YOLOv8n or s, track runs in Weights and Biases, use augmentation for blur, brightness and rain.
- **Export:** ONNX or TFLite, INT8 or FP16, and record the accuracy change after quantization.
- Start training early. It takes the longest to get right.

## 13. Evaluation plan

| Experiment | Metric |
| --- | --- |
| E1 Detector on a route-split test set | Precision, recall, PR curve. The report's target was about 0.85 recall at 0.70 precision; **\[To verify\]** re-check it against the current plan. |
| E2 Pothole-free stretches | False alarms per km. |
| E3 Real phone, 15 to 20 minutes | Sustained FPS, temperature, battery drain. |
| E4 Verification on replay set | Gated verification versus naive absence; false closures shown as counts. |
| E5 Location error | Median and 90th percentile distance from a marked pothole. |
| E6 Offline test | Queue survives airplane mode, uploads once, no duplicates. |

Rules: show counts with percentages, state the test set size, and label every simulated case.

## 14. Privacy, safety and ethics

- Road-only crops: no faces or plates by design. Check crops anyway during testing.
- Explicit consent screen in the app; explain what is stored and how to delete it.
- Minimise location history: trim raw tracks, keep only what verification needs.
- Driver safety: the app must never require attention while driving.
- Emails go to a test inbox only. In production, authority contacts must be verified and the sender identity and opt-out handled properly.
- Fixed claims by authorities are never trusted blindly; verification is the safeguard.

## 15. Risks and fallbacks

| Risk | Likelihood | Impact | Mitigation and fallback |
| --- | --- | --- | --- |
| Phone overheats or throttles | High | Medium | Lower inference rate, charger, test early. Fallback: laptop in the car with phone GPS. |
| Android camera or background service issues | Medium | High | Build a bare camera-plus-model screen in the first days. |
| Poor model accuracy | Medium | High | Hard negatives, own footage, tune thresholds. |
| GPS error breaks duplicate merge | Medium | Medium | Measure error, adjust radius, state the limit. |
| Too few real repairs for verification stats | High | Medium | Report counts, label replays as simulated. |
| Hardware (Pi, Hailo) late or toolchain fails | High | Low | Optional only; CPU or phone as the demo target. |
| Free-tier limits or outages | Medium | Medium | Record a backup demo video; run the stack locally. |
| Licence problem (AGPL YOLO) | Medium | High for commercial use | Decide on enterprise licence or permissive detector before pitching sales. |

## 16. Build plan

### 16.1 Order

1. Model plus a laptop script that reads a recorded drive and prints detections.
2. Backend and database that accept events and show them in a table.
3. Website map that displays them.
4. Email to a test inbox.
5. Verification flow (Fixed link, passes, reopen). Do not leave it for the last hour.
6. Android app wrapping the tested model, with the offline queue.
7. Polish and a recorded backup demo.

Building on recorded video first means the app only has to produce the same events.

### 16.2 Team split

- **Three people:** Person 1 Android app; Person 2 backend, database and verification; Person 3 model training and website.
- **Solo:** follow the order above. Cut Android polish before cutting verification.

### 16.3 Definition of done (MVP)

- The full demo script in 4.2 runs from a clean start without manual fixes.
- Numbers for E1 to E6 exist, or are clearly marked as not done.
- The offline queue works with no duplicates.
- A backup demo video exists.

### 16.4 Day-before checklist

- Charged phone, mount, cable and mobile hotspot.
- Replay clip and GPS log tested on the demo laptop.
- Test inbox open and email delivery checked.
- Production database seeded with a clean demo state, plus a reset script.
- Backup video on a USB drive and a phone.
- Slides with the real-versus-simulated table and the numbers.

## 17. Roadmap and business

| Phase | Focus |
| --- | --- |
| 0. Hackathon | Android app, verification demo, honest numbers. |
| 1. Fleet pilot | Cabs, delivery vehicles or school buses with phones or cheap Android cameras; measure real roads. |
| 2. SDK | Licence to Android and Linux camera makers, with the cloud service. |
| 3. Scale | Larger dashcam makers and OEMs; better GNSS and IMU; probabilistic verification; multi-vehicle consensus. |

**Revenue ideas \[Assumed, to validate\]:** fleet subscription, SDK licensing, and a service for authorities. Interview fleet managers and dashcam resellers about willingness to pay and chips used.

## 18. Appendix

### 18.1 Glossary

- **Event:** a confirmed pothole sighting from one device.
- **Pothole:** a merged record of one or more events at one place.
- **Pass:** one drive over a road stretch, with quality flags.
- **Valid clean pass:** a pass where the camera could really have seen the pothole.
- **Verified:** claimed fixed and then confirmed by clean passes.
- **False closure:** a pothole marked verified that was not actually fixed.

### 18.2 Decision log

| Date | Decision | Reason |
| --- | --- | --- |
| 2026-10-25 | Repair verification is a Must | It is the differentiator. |
| 2026-10-25 | Android phone first, native Kotlin | Removes hardware risk. |
| 2026-10-25 | Pi with Hailo is optional | Schedule and toolchain risk. |
| 2026-10-25 | FastAPI, Postgres with PostGIS, Next.js with MapLibre | Fast to build, strong map support. |
| 2026-10-25 | Severity is graded in bands | No depth claims. |
| 2026-10-25 | Test inbox only | No real authority spam. |
| 2026-10-25 | Simple distance merge, no H3 or map matching in MVP | Scope. |
| Open | Detector licence route | Decide before any sales pitch. |

### 18.3 Open questions

- How many hours do we have and how many people are on the team?
- Which city and which authority contact do we configure?
- Which Android phone is the test device?
- Which detector licence route?
- Licences and terms of the chosen datasets.

### 18.4 Environment variables

DATABASE\_URL, STORAGE\_BUCKET, STORAGE\_KEY, RESEND\_API\_KEY, TEST\_INBOX, ADMIN\_SECRET, PUBLIC\_BASE\_URL, FIXED\_TOKEN\_SALT.

### 18.5 References to verify

- Prior art: iWatchRoadv2 and the Pothole Reporter project named in the original report.
- Ultralytics YOLO licence terms.
- Hailo and Raspberry Pi compatibility for the chosen model.
- Sources for the problem statistics in 1.2.
- Licences of the public datasets used.

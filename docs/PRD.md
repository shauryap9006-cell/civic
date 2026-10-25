# CivicLens — Product Requirements Document (PRD)

Version: 0.1 (draft)
Date: 25 October 2026
Status: Hackathon MVP scope, ready for team review
Companion document: CivicLens — Project Overview and Build Plan (architecture, algorithms, data model, API)

Priority legend
- P0 = must exist for the MVP demo
- P1 = should have if time remains
- P2 = wow / stretch
- P3 = later (roadmap, not built now)

Evidence tags used in this file
- [Assumed] design choice or target not yet tested
- [Measured] we ran it and have numbers
- [Simulated] shown through replay or a mock in the demo
- [To verify] taken from a third party or memory; check before it goes on a slide

---

## 1. Purpose

This document defines what CivicLens must do, for whom, and how we will know it works. It is the contract between the three build tracks (Android app, backend and verification, model and website). Implementation detail lives in the Project Overview and Build Plan.

## 2. Background and problem

- Potholes damage vehicles, cause accidents and waste fuel. Citizens can complain, but complaints are often marked resolved without anyone confirming the road was repaired.
- Existing apps and research systems mostly detect and report potholes. The unsolved part is closing the loop: proving whether a repair really happened and reopening the case if it did not.
- [To verify] Add one or two sourced statistics (accidents linked to potholes, share of complaints closed without repair) with links before any pitch.
- [To verify] Prior art named in the earlier report (iWatchRoadv2, which has a basic repair check, and a Pothole Reporter project) must be re-checked before slides.

## 3. Vision and goals

Vision: any Android phone (later, any Android or Linux camera) becomes a road-condition sensor that reports potholes and verifies repairs with evidence.

Goals
1. G1: Deliver one working end-to-end loop: detect, report, Fixed claim, later clean pass, verified or reopened with before/after evidence.
2. G2: Show honest, measured numbers (FPS on a real phone, false alarms per km, false closures as counts).
3. G3: Show a credible path from a phone app to a licensed module for fleets and camera makers.

Differentiator: repair verification gated by pass quality. Not seeing a pothole counts as evidence only when the camera could really have seen it. We do not claim to be first or that nobody else verifies repairs; we claim that others check naively and we gate by pass quality.

## 4. Non-goals (MVP)

- Measuring pothole depth or volume. Severity is graded in bands only.
- Sending emails to real authorities. A test inbox only.
- iOS app.
- Installing on closed dashcams or factory car cameras.
- Training a detector from scratch.
- Map matching, contractor/defect-liability links, contributor leaderboard, over-the-air model updates, multi-vehicle consensus (simulated by replay only).
- Kafka, Kubernetes, microservices, GraphQL, blockchain.

## 5. Users and personas

| ID | Persona | Needs | Key behaviour |
|---|---|---|---|
| PE-1 | Driver (contributor) | Hands-free detection, no distraction, reports saved without signal | Mounts the phone, starts a drive, glances at a count only when stationary |
| PE-2 | Authority officer | One clear report per pothole, easy way to mark repair done | Clicks a link from an email, no login |
| PE-3 | Citizen | See what is reported and whether it is really fixed | Browses the public map and history |
| PE-4 | Fleet manager (later) | Aggregate coverage and results across many vehicles | Deploys devices, reads summaries |
| PE-5 | Judge / reviewer | Understand and trust the evidence behind a status | Watches the demo, inspects a detail page |

## 6. User stories

| ID | Story | Priority |
|---|---|---|
| US-01 | As a driver, I start a drive and the phone detects potholes without me touching it. | P0 |
| US-02 | As a driver with no signal, I want reports queued and sent when I am online again, without duplicates. | P1 (offline demo), P0 (basic upload) |
| US-03 | As an officer, I get one email per pothole with a photo, a map link and a severity band. | P0 |
| US-04 | As an officer, I click one link to say the repair is done. | P0 |
| US-05 | As a citizen, I see a pothole's status (open, reported, claimed fixed, verified, reopened) on a map. | P0 |
| US-06 | As a citizen or judge, I open a pothole and see the evidence behind its status, including before and after. | P0 |
| US-07 | As a driver, I can see how many potholes my drives reported. | P1 |
| US-08 | As an officer, I get a reminder if a reported pothole has no repair claim after a set time. | P1 |
| US-09 | As a driver, I understand what is stored and can stop sharing. | P0 (consent screen) |

---

## 7. Functional requirements

### 7.1 Detection and tracking (DET)

| ID | Requirement | Priority |
|---|---|---|
| DET-01 | The app shall run a pothole detector on live camera frames of the road ahead. | P0 |
| DET-02 | The app shall track candidates across frames and create an event only after a candidate is tracked for at least 3 frames [Assumed, tune]. | P0 |
| DET-03 | The detector shall be fine-tuned on public pothole data plus own footage, with hard negatives (shadows, manhole covers, speed breakers, patches, wet surfaces). | P0 |
| DET-04 | The model shall export to ONNX or TFLite (INT8 or FP16) and the accuracy change after quantization shall be recorded. | P0 |
| DET-05 | The same detection core shall run on a laptop (for replay) and on the phone. | P0 |
| DET-06 | The app shall adapt inference rate when the phone reports thermal throttling. | P1 |
| DET-07 | The same core shall run on a Raspberry Pi 5 (CPU or Hailo) as a portability demonstration. | P2 |

Acceptance (DET-01, DET-02): on a recorded drive replayed through the core, each pothole produces at most one event per pass, and isolated single-frame detections produce no event.

### 7.2 Event creation (EVT)

| ID | Requirement | Priority |
|---|---|---|
| EVT-01 | An event shall contain: id (UUID generated on device), time, GPS point, GPS accuracy, heading, speed, confidence, track length, severity band, road-only crop and model version. | P0 |
| EVT-02 | Only a crop of the road region shall be saved. Full wide frames shall not be stored or uploaded. | P0 |
| EVT-03 | Each event shall be signed with the device's Ed25519 key; the backend shall reject events with an invalid signature. | P0 |
| EVT-04 | Severity shall be one of three bands (low, medium, high) and shall be described everywhere as graded, not measured. | P0 |

Acceptance (EVT-03): a payload altered after signing is rejected and the rejection reason is visible in a log.

### 7.3 Offline queue and sync (OFF)

| ID | Requirement | Priority |
|---|---|---|
| OFF-01 | Events and pass logs shall be stored in a local database and uploaded when connectivity is available. | P0 |
| OFF-02 | Uploads shall be idempotent, using the event id, so retries never create duplicate potholes. | P0 |
| OFF-03 | Retries shall use exponential backoff with jitter. | P1 |
| OFF-04 | The demo shall show the queue surviving airplane mode and uploading once afterwards. | P1 |

Acceptance (OFF-04): 5 events captured offline appear exactly once each on the server after reconnection.

### 7.4 Duplicate merge (MRG)

| ID | Requirement | Priority |
|---|---|---|
| MRG-01 | The backend shall merge an event into an existing open pothole if it lies within 10 m [Assumed, tune]; otherwise it shall create a new pothole. | P0 |
| MRG-02 | The canonical pothole location shall be recomputed as the confidence-weighted mean of its events. | P1 |
| MRG-03 | The measured GPS error shall be reported, and the merge radius adjusted to it. | P1 |

### 7.5 Reporting to authorities (RPT)

| ID | Requirement | Priority |
|---|---|---|
| RPT-01 | The system shall send exactly one initial email per pothole, to a configured test inbox. | P0 |
| RPT-02 | The email shall include a photo, a map link, a severity band, the pothole id and a Fixed link. | P0 |
| RPT-03 | Authority contacts shall be held in a configuration for one city. | P1 |
| RPT-04 | The system shall record each email (kind, time, provider message id, status). | P0 |

Acceptance (RPT-01): ten events merged into one pothole produce one email, not ten.

### 7.6 Repair verification (VER) — the core differentiator

| ID | Requirement | Priority |
|---|---|---|
| VER-01 | Each drive shall upload pass records: the path driven and per-segment quality flags (blur, occlusion, light, speed, camera pitch). | P0 |
| VER-02 | The backend shall compute, for each known pothole, whether a pass was a valid clean pass or unknown. | P0 |
| VER-03 | A valid clean pass requires: the path within roughly 5–25 m of the pothole [Assumed], sharpness above threshold, no occlusion flag, adequate light, and GPS accuracy sufficient to place the camera on that road. | P0 |
| VER-04 | The authority's Fixed click shall set the status to claimed fixed. It is a claim and not proof. | P0 |
| VER-05 | A pothole shall become verified when at least 2 valid clean passes occur after the claim with no detection within the merge radius [Assumed, tune]. | P0 |
| VER-06 | If passes occur after the claim but none is valid, the status shall be unknown, not verified. | P0 |
| VER-07 | Any confident detection within the merge radius after the claim shall set the status to reopened and attach before and after evidence. | P0 |
| VER-08 | Every status change shall be written to a history with its evidence (event ids and pass ids). | P0 |
| VER-09 | A probabilistic (log-odds) verification model using measured per-condition false-negative rates shall be considered later. | P3 |

Acceptance (VER-05 to VER-07), on a labelled replay set: a clean pass that did not see the pothole verifies; a blurred or occluded pass never verifies; a later detection always reopens. Report results as counts (for example 3 of 4 repairs verified, 0 false closures out of 5 unfixed cases) and label replays as [Simulated].

### 7.7 Escalation (ESC)

| ID | Requirement | Priority |
|---|---|---|
| ESC-01 | If a reported pothole has no Fixed claim after N days (start 14), the system shall send a reminder. | P1 |
| ESC-02 | After a second interval, the system shall escalate to a configured escalation contact. | P1 |
| ESC-03 | A protected demo endpoint shall advance the clock to show escalation. [Simulated] | P1 |

### 7.8 Android app (APP)

| ID | Requirement | Priority |
|---|---|---|
| APP-01 | The app shall have three screens: drive (preview, Start/Stop, status), settings (consent, test city), trip summary. | P0 |
| APP-02 | Detection shall continue in a foreground service with a persistent notification. | P0 |
| APP-03 | The status line shall show GPS lock, FPS and number of queued reports. | P1 |
| APP-04 | Start and Stop shall be usable only while stationary; nothing shall require attention while driving. | P0 |
| APP-05 | A consent screen shall explain what is stored and how to stop sharing. | P0 |
| APP-06 | The app shall request only camera, fine location, notification and foreground-service permissions. | P0 |
| APP-07 | A companion pairing flow (QR) shall be defined for non-phone devices. | P3 |

### 7.9 Website (WEB)

| ID | Requirement | Priority |
|---|---|---|
| WEB-01 | A public map shall show potholes as pins coloured by status (open, reported, claimed fixed, verified, reopened) with a legend; colour shall not be the only cue (add labels or icons). | P0 |
| WEB-02 | A pothole detail page shall show the status timeline, before and after photos, the pass log and the evidence behind each status. | P0 |
| WEB-03 | An authority Fixed page shall show the pothole and one confirm button, with no login. | P0 |
| WEB-04 | An impact counter shall show reports sent, repairs verified and cases reopened. | P1 |
| WEB-05 | A demo-only skip-time control shall be available only in demo mode. | P1 |

### 7.10 API and access (API)

| ID | Requirement | Priority |
|---|---|---|
| API-01 | The API shall provide: device registration, upload signing, events upload, passes upload, potholes list and detail, Fixed preview and claim, stats, and an admin simulate-time endpoint (see the Project Overview for the endpoint table). | P0 |
| API-02 | Device requests shall be signed; public reads shall not need auth. | P0 |
| API-03 | Fixed links shall use unguessable tokens and only a token hash shall be stored. | P0 |
| API-04 | The simulate-time endpoint shall require an admin secret and be disabled in production. | P0 |

---

## 8. Non-functional requirements

| ID | Area | Requirement |
|---|---|---|
| NFR-01 | Performance | Sustained inference of at least 10 FPS on the test phone for 15 to 20 minutes [Assumed]; report the actual number and temperature. |
| NFR-02 | Performance | Event appears on the website within 30 seconds of upload when online [Assumed]. |
| NFR-03 | Reliability | No duplicate potholes or duplicate emails from retried uploads. |
| NFR-04 | Reliability | A crash or restart of the app shall not lose queued events. |
| NFR-05 | Security | Signed device payloads; hashed Fixed tokens; admin endpoint protected; secrets only in environment variables. |
| NFR-06 | Privacy | Road-only crops; consent screen; trim raw GPS tracks after verification needs are met; no full wide frames stored. |
| NFR-07 | Usability | A driver shall never need to look at or touch the phone while the vehicle is moving. |
| NFR-08 | Compatibility | Android first. Define a minimum Android version after testing on the chosen device [Open]. |
| NFR-09 | Accessibility | Map status shall be readable without colour alone. |
| NFR-10 | Observability | Log signature rejections, upload retries, email results and verification decisions with their evidence. |
| NFR-11 | Licensing | Ultralytics YOLO is AGPL-3.0 [To verify]. Before any commercial pitch, decide between their enterprise licence and a permissively licensed detector. Check dataset licences too. |
| NFR-12 | Deployability | One docker compose up shall start the backend, database and storage emulator locally. |

---

## 9. Success metrics

Hackathon acceptance targets (starting values, [Assumed], to be replaced by measured results)

| Metric | Target | How measured |
|---|---|---|
| Detection recall at acceptable precision | About 0.85 recall at 0.70 precision (target from the earlier report, [To verify]) | Route-split test set |
| False alarms | No more than 2 per km on pothole-free stretches | Replay of clean roads |
| Sustained FPS | At least 10 on the test phone for 15 to 20 minutes | Real device run |
| Location error | Report median and 90th percentile; no target set until measured | Marked pothole vs GPS point |
| Verification | Zero false closures on the replay set; report counts | Labelled replay set |
| Duplicate emails | Zero | Merge test with 10 events |
| Offline sync | 100% of queued events uploaded exactly once | Airplane-mode test |

Product metrics for later pilots: verified repairs, reopen rate, time from report to claim, time from claim to verification, kilometres covered per device.

---

## 10. Data requirements

- Entities: devices, authorities, events, potholes, passes, pass_coverage, reports, status_history (columns and indexes in the Project Overview).
- Storage: Postgres with PostGIS for locations; object storage for crops.
- Retention [Assumed]: keep crops for a fixed period after a pothole is verified; trim raw GPS tracks; never store wide frames. Define exact periods before any pilot.
- Training data: public pothole datasets plus own footage. The earlier report suggested about 20 km of footage, about 500 labelled frames and 15 to 20 revisited pothole locations; scale this to the time available. Split by route, not by frame.

---

## 11. Release plan

| Slice | Contents |
|---|---|
| MVP (P0) | Detector, tracker and gate; signed events; backend with merge; test-inbox email; map and detail page; pass logging and verification; Fixed link; reopen with before/after; Android app (drive screen, consent, foreground service). |
| Should (P1) | Offline queue demo; one-city authority config; escalation timers with skip-time; trip summary and counters; thermal adaptation; merge refinement. |
| Wow (P2) | Pi 5 with Hailo demo; IMU confirmation; two or three evaluation charts (gated verification versus naive absence). |
| Later (P3) | Fleet pilot; SDK for Android and Linux camera makers; companion pairing for non-phone devices; probabilistic verification; map matching; multi-vehicle consensus; larger makers and OEMs. |

Build order: model and replay script; backend and database; website map; email; verification flow; Android app; polish and backup demo video.

Demo definition of done: the script (problem, live or replayed detection, email in inbox, map and detail page, Fixed click, verified, reopened with before/after, offline queue, numbers and roadmap) runs from a clean start in about 3.5 minutes without manual fixes, and a backup video exists.

---

## 12. Dependencies and assumptions

Dependencies
- A test Android phone with camera, GPS and a mount.
- A recorded drive video with a GPS log for replay.
- Accounts: Postgres host, object storage, email provider (test inbox).
- Labelled data and a trained model exported for mobile.

Assumptions
- Team size and hours are not yet fixed [Open].
- The demo uses a test inbox and a configured city.
- Real repairs may be too few to prove verification statistically; replays will be labelled [Simulated].
- Free-tier services are sufficient for the demo.

---

## 13. Risks

| Risk | Likelihood | Impact | Mitigation / fallback |
|---|---|---|---|
| Phone overheats or throttles | High | Medium | Lower inference rate, charger, early test. Fallback: laptop in the car with phone GPS. |
| Android camera or background service problems | Medium | High | Build a bare camera-plus-model screen in the first days. |
| Poor model accuracy | Medium | High | Hard negatives, own footage, tune thresholds. |
| GPS error breaks duplicate merge | Medium | Medium | Measure error, adjust radius, state the limit. |
| Too few real repairs for statistics | High | Medium | Report counts and label replays. |
| Pi/Hailo late or toolchain fails | High | Low | Optional; phone or CPU is the demo target. |
| Free-tier outage | Medium | Medium | Run the stack locally; backup video. |
| AGPL licence blocks commercial use | Medium | High (commercial) | Decide licence route before any sales pitch. |

---

## 14. Open questions

1. How many hours do we have, and how many people are on the team?
2. Which city and which authority contact do we configure?
3. Which Android phone is the test device, and what minimum Android version do we support?
4. Which detector licence route do we take?
5. What are the licences and terms of the chosen datasets?
6. What are the exact retention periods for crops and tracks?
7. Do we need real repair examples, or is a labelled replay set enough for the submission?

---

## 15. Glossary

- Event: a confirmed pothole sighting from one device.
- Pothole: a merged record of one or more events at one place.
- Pass: one drive over a road stretch, with quality flags.
- Valid clean pass: a pass where the camera could really have seen the pothole.
- Claimed fixed: the authority says it is repaired; not yet proof.
- Verified: claimed fixed and then confirmed by clean passes.
- Reopened: a later detection contradicts a fixed claim.
- False closure: a pothole marked verified that was not actually fixed.

---

## 16. Change log

| Date | Version | Change |
|---|---|---|
| 2026-10-25 | 0.1 | First draft from the report review and planning discussion. |

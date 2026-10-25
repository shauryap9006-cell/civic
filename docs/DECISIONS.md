# DECISIONS

Status: living log. One row per decision. When you change a decision, add a new row that supersedes the old one (do not delete history). Agents: if you must change a contract, threshold, tech choice or rule, add an entry with the evidence.

| ID | Date | Decision | Reason / evidence | Status |
|---|---|---|---|---|
| D-01 | 2026-10-25 | Repair verification is a Must, not a Wow | It is the differentiator; without it the project is detection plus email | Active |
| D-02 | 2026-10-25 | Android phone first, native Kotlin | Removes hardware risk; reliable background camera and GPS; iOS limits background camera use | Active |
| D-03 | 2026-10-25 | Raspberry Pi 5 / Hailo is optional (portability proof only) | Shipping time and toolchain risk | Active |
| D-04 | 2026-10-25 | Backend: FastAPI + PostgreSQL/PostGIS; site: Next.js + MapLibre | Fast to build; strong spatial queries | Active |
| D-05 | 2026-10-25 | Severity is a graded band (low/medium/high), never a measurement | No depth/volume claims are supportable from a single image | Active |
| D-06 | 2026-10-25 | Emails go to a test inbox only (`EMAIL_MODE=test`) | Do not spam real authorities during the hackathon | Active |
| D-07 | 2026-10-25 | Simple distance merge (`ST_DWithin`, 10 m); no H3 or map matching in the MVP | Scope; revisit after measuring GPS error (E5) | Active |
| D-08 | 2026-10-25 | Detector licence route undecided: Ultralytics YOLO is AGPL-3.0 **[To verify]** | Fine for the hackathon; before commercial use choose the vendor's enterprise licence or a permissive detector (RT-DETR/RF-DETR variant, verify licence) | **Open** |
| D-09 | 2026-10-25 | Rule-based verification (count of valid clean passes) instead of a probabilistic model | Easier to explain, test and demo; log-odds belief update is a roadmap item | Active |
| D-10 | 2026-10-25 | Event and pass bodies are signed as exact payload strings (envelope `{payload, signature}`), plus a request-level signature | Avoids JSON canonicalisation bugs; keeps the exact signed bytes for audit | Active |
| D-11 | 2026-10-25 | Fixed token is derived: `HMAC(FIXED_TOKEN_SECRET, pothole_id:epoch)`; only `sha256(token)` is stored; epoch increments on reopen | Reminders can reuse the link, reopen supersedes it, no plaintext token in the database | Active |
| D-12 | 2026-10-25 | Verification is recomputed from stored data (idempotent), not incremented | Out-of-order and offline uploads must give the same result | Active |
| D-13 | 2026-10-25 | Pass samples at 5 Hz (`sample_interval_s: 0.2`) | At about 14 m/s a 20 m approach window would hold only 1–2 samples at 1 Hz | Active |
| D-14 | 2026-10-25 | Only a road-region crop is stored; no wide frames | Privacy by construction | Active |
| D-15 | 2026-10-25 | Occlusion check uses a `vehicle` class from the detector; it can be disabled and the UI then says "occlusion not checked" | Honest fallback if vehicle labelling is too costly | Active |
| D-16 | 2026-10-25 | One shared `config/thresholds.yaml`; no hard-coded thresholds | Tuning without code changes; tests stay meaningful | Active |
| D-17 | 2026-10-25 | Ed25519 via libsodium/Tink with a Keystore-wrapped private key on Android | Hardware-backed Ed25519 is not universally available | Active |
| D-18 | 2026-10-25 | Escalation and re-evaluation use an in-process scheduler, no message broker | Hackathon scope | Active |

## Entry template

| ID | Date | Decision | Reason / evidence (measurements, links, test names) | Status (Active / Superseded by D-xx / Open) |
|---|---|---|---|---|

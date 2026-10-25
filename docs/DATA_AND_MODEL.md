# DATA AND MODEL

Status: v0.1. Covers class definition, labelling guide, datasets, collection plan, training, export, calibration and licences. Anything marked **[To verify]** must be checked before it is relied on or put on a slide.

## 1. Classes

| Class | Purpose | Notes |
|---|---|---|
| `pothole` | Produces events | A bowl-shaped break or hole in the road surface that a vehicle can drop into or must swerve around |
| `vehicle` | Occlusion estimate only | Cars, buses, trucks, two-wheelers that block the road view; never produces events |

If labelling vehicles is too costly, set `pass_quality.occlusion_check_enabled: false`. The UI then says "occlusion not checked" on every verification (see STATE_MACHINE §4.3). This is an honest, documented limitation, not a silent skip.

## 2. Labelling guide (write this on one page and attach 10 example images before anyone labels)

### 2.1 Label as `pothole`
- Clear holes and broken-away asphalt with a visible edge, any size at least roughly 20 cm across.
- Water-filled potholes **if** the edge of the hole is visible.
- Clusters: label each distinct hole; if edges merge into one damaged patch, label one box around the merged damage.
- Partially visible holes at the frame edge if more than half is visible.

### 2.2 Do not label (these are hard negatives; include such frames in training with no box)
- Shadows (trees, poles, vehicles, buildings) and dark stains.
- Manhole covers, drain grates, utility covers, road studs.
- Speed breakers and rumble strips.
- Well-patched asphalt with a flat, intact surface; painted markings; skid marks.
- Wet reflections on intact asphalt; mud and puddles with no visible hole edge.
- Cracks (long thin cracks) unless they have broken out into a hole.
- Anything beyond the road region of interest (`detection.roi`).

### 2.3 Box rules
- Tight box around the hole including its broken edge; no padding.
- When unsure whether an object qualifies: **skip the frame for training and note the id** in `ml/labelling_uncertain.csv` rather than guessing; two people review the uncertain list weekly.
- Vehicles (for occlusion): label cars, buses, trucks, motorbikes, auto-rickshaws, and large pedestrians/animals only if they cover road area; one box per object; occlusion ratio is computed from the union of boxes inside the ROI.

### 2.4 Quality control
- Two labellers independently label 10% of the images; compute agreement (box IoU ≥ 0.5 match rate). If agreement is below 0.8, revise the guide before continuing.
- Keep the guide version in every dataset export.

## 3. Datasets

| Source | Use | Check before use |
|---|---|---|
| Public pothole datasets on Roboflow Universe and Kaggle **[To verify: pick specific ones]** | Pre-training and diversity | Licence terms (commercial use allowed?), label quality, camera angle (dashcam-like vs pavement close-ups), country/road type |
| Own footage from the target roads | Fine-tuning, validation and test | Collected under section 4 |
| COCO-pretrained detector | Optional pseudo-labels for `vehicle` | Review a sample by hand; correct systematic errors |

Rules:
- Record every source, licence, count, and download date in `ml/DATASET.md`.
- Do **not** train on frames from the routes used for the test set.
- Split **by route/drive, not by frame**: near-identical consecutive frames must not appear in both train and test.
- Suggested split of own footage: 60% train, 20% validation, 20% test (by drive). Keep at least one complete route as a final held-out test.
- Include night and rain frames only in proportions you can also evaluate; otherwise note that the model is untested there.

## 4. Data collection plan (own footage)

**Safety first:** the driver never operates the phone. A passenger starts and stops recording, or the app is started while parked. Follow local traffic law for windscreen mounts.

- **Target (from the earlier report, scale to your time) [To verify against your schedule]:** about 20 km of footage, about 500 labelled frames, and 15–20 pothole locations that you can revisit.
- **Routes:** pick 2–3 routes of 3–10 km with a mix of good and bad road; drive each at least twice at different times (morning, afternoon) and, if possible, once in different lighting.
- **Ground truth for locations (E5):** for each of the 15–20 potholes, take a photo with a landmark, drop a map pin (or record a careful GPS reading standing at the pothole) and note a short description. Store in `ml/ground_truth.csv` (id, lat, lon, description, photo, date).
- **Revisits:** revisit the same potholes over days or weeks. Note any that get repaired (date, photo) in `ml/repairs.csv`. Real repairs are rare; never invent them.
- **Mount:** the phone fixed at the windscreen centre, horizontal, view covering the lane ahead; record the mount position and phone model in `ml/capture_log.csv`.
- **Metadata per drive:** date, time, weather, route, phone, app/model version, GPX file, video file.
- **Pass quality calibration set:** record short clips of good, blurred (rough road), dark (dusk), and occluded (following a truck) conditions to calibrate `min_sharpness`, `min_mean_luma` and `max_occlusion_ratio`. Record the chosen values and the evidence in `docs/DECISIONS.md`.

## 5. Training

- Framework: Ultralytics YOLO, size n first (fast on phones), s if recall is too low and FPS allows. **Licence:** AGPL-3.0 **[To verify]**. Fine for the hackathon; before any commercial pitch decide between the vendor's enterprise licence and a permissively licensed detector (D-08).
- Input size `detection.model_input_px`. Start from COCO or a public pothole-pretrained checkpoint.
- Augmentation: brightness/contrast, motion blur, rain/noise, small rotations and scale; no vertical flips; horizontal flip is acceptable.
- Class balance: keep a large share of negative (no-box) frames to teach the model about shadows and manhole covers.
- Track runs (parameters, metrics, dataset hash) in Weights and Biases or a local CSV.
- Select the model by validation **precision at the recall you need** and by **false events per km after the gate** (E2), not by mAP alone.

## 6. Export and on-device performance

- Export to ONNX (laptop/Pi) and LiteRT (Android). Try FP16 first, then INT8 with a representative calibration set of ~200 real frames.
- Record accuracy before and after quantization on the same validation set (`DET-04`).
- Measure FPS on the real phone for 15–20 minutes (E3) with the camera running; report median and 5th-percentile FPS and temperature. Do not quote a peak number.
- If the sustained FPS is below `detection.min_fps`: use the nano size, reduce `model_input_px`, or lower `target_fps` and record the decision.
- Model versioning: `model_version` = `<size>-<date>-<short hash>`; sent in every event and pass.

## 7. Calibrating the quality metrics

1. Compute `sharpness`, `mean_luma` and `occlusion_ratio` for the calibration clips with the shared metric code.
2. Plot or tabulate the distributions for good vs degraded conditions.
3. Choose thresholds that separate them with a margin; prefer a stricter threshold (fewer clean passes) over a looser one, because false closures are worse than `unknown`.
4. Record the values, the clips used, and the date in `docs/DECISIONS.md`; update `config/thresholds.yaml`.

## 8. Replay set for verification evaluation (T-60)

A labelled collection of passes over known potholes:
- Real: your own passes before and after any real repair; passes in different conditions (clean, blurred, dark, behind a truck).
- Scripted: passes generated by the replay tool with controlled degradation (for example blur filter applied to a clean clip, brightness reduced, a vehicle crop pasted in) **clearly labelled `simulated`**.
Each case has: pothole id, expected outcome (should/should not verify), condition, real or simulated, file and GPX. Results (E4) show counts for both categories separately.

## 9. Licences and attribution checklist

| Item | Action |
|---|---|
| Ultralytics YOLO | Decide licence route; record in D-08 |
| Each public dataset | Record licence; confirm training and any redistribution terms; keep attribution text |
| Map tiles | Use tiles whose terms allow your use (OSM-based provider; show attribution) |
| Fonts/icons | Use open licences |
| Third-party libraries | Keep a `THIRD_PARTY.md` with licences before any pilot |

## 10. Privacy of training data

Footage of public roads can include faces and plates. Keep raw footage private; store only crops needed for events; if you share a dataset, blur faces and plates first. Delete raw footage after the submission period unless needed.

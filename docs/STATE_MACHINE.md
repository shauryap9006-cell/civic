# STATE MACHINE AND VERIFICATION

Status: v0.1. This is the highest-priority document in the pack. Implement it exactly; test it before wiring it to the database.
All numbers come from `config/thresholds.yaml` (keys quoted like `verification.required_clean_passes`). All values are [Assumed] starting points.

## 1. Statuses

| Status | Meaning | Counts as repaired? |
|---|---|---|
| `open` | First event received; authority not yet notified | No |
| `reported` | Authority notified (initial or reopened email sent); waiting for a repair claim | No |
| `claimed_fixed` | Authority clicked Fixed. A claim, not proof. No decisive passes yet | **No** |
| `unknown` | Passes after the claim exist, but none was a valid clean pass (camera could not really see) | **No** |
| `verified` | Enough valid clean passes after the claim saw no pothole | **Yes** (the only status that does) |
| `reopened` | New evidence contradicts a claim or a verification; authority will be re-notified | No |

`reopened` is transient: after the "reopened" email is sent the pothole moves to `reported` (a new escalation cycle starts).

## 2. Transitions

| # | From | To | Trigger | Guard | Side effects | `reason` code |
|---|---|---|---|---|---|---|
| T1 | (none) | `open` | First event with no pothole within `merge.radius_m` | event passed ingest checks | Create pothole; set `before_event_id` candidate; queue initial email | `first_event` |
| T2 | `open` | `reported` | Initial report email sent successfully | a `reports` row of kind `initial` is `sent` | Set `reported_at`, `last_reported_at`; mint Fixed token | `report_sent` |
| T3 | `reported` | `claimed_fixed` | `POST /v1/fixed/{token}` with the current token | token valid and current | Set `claimed_fixed_at = clock.now()`; choose `before_event_id`; clear `verified_at` | `fixed_claim` |
| T4 | `claimed_fixed` | `verified` | Re-evaluation | ≥ `verification.required_clean_passes` distinct valid clean passes with crossing time after `claimed_fixed_at` and no contradicting event | Set `verified_at` | `clean_passes_met` |
| T5 | `claimed_fixed` | `unknown` | Re-evaluation | at least one pass crossing after the claim exists, none is clean, and T4 is not met | none | `no_valid_clean_pass` |
| T6 | `unknown` | `verified` | Re-evaluation | same guard as T4 | Set `verified_at` | `clean_passes_met` |
| T7 | `claimed_fixed` / `unknown` / `verified` | `reopened` | Event merged into this pothole | `event.captured_at > claimed_fixed_at` and `event.confidence ≥ verification.reopen_min_event_confidence` and count of such events ≥ `verification.reopen_min_events` | Set `reopened_at`; clear `verified_at`; queue "reopened" email with before/after evidence | `contradicting_event` |
| T8 | `reopened` | `reported` | "reopened" email sent | a `reports` row of kind `reopened` is `sent` | Set `last_reported_at`; rotate the Fixed token (the old token now returns 410). `claimed_fixed_at` keeps its old value until a new claim overwrites it; a reopened or re-reported pothole is never evaluated for verification, so the old value is inert | `reopen_report_sent` |
| T9 | `reported` | `reported` | Escalation timer | `now - last_reported_at ≥ escalation.reminder_after_days` and no reminder this cycle | Send `reminder` email | `reminder_sent` |
| T10 | `reported` | `reported` | Escalation timer | `now - last_reported_at ≥ escalation.escalate_after_days` and no escalation this cycle | Send `escalation` email to `authorities.escalation_email` (or test inbox) | `escalation_sent` |

T9 and T10 do not change `status`; they write a `status_history` row with `from_status = to_status = reported` so the timeline shows them.

### Not allowed (must be rejected or ignored, never applied)

- `open → claimed_fixed` (no token exists before the report is sent).
- Any transition into `verified` from a status other than `claimed_fixed` or `unknown`.
- `verified → claimed_fixed` or `verified → unknown` (only `reopened` can leave `verified`).
- Any write to `potholes.status` outside `logic/state_machine.py` / `services/potholes.py`.
- Counting a pass as clean if it was captured before the claim.

### Events that do not change status

- Events attached to `open`, `reported` or `reopened` potholes: increase `event_count`, recompute location and severity, no status change.
- Events with `captured_at ≤ claimed_fixed_at`: pre-claim evidence (for example uploaded late by an offline device). Attach, never reopen.
- Events below `reopen_min_event_confidence` after a claim: attach, do not reopen.

## 3. Claim endpoint behaviour

| Current status | Token | Result |
|---|---|---|
| `reported` | current | T3, return 200 with new state |
| `claimed_fixed`, `unknown`, `verified` | current | 200, return current state unchanged (idempotent, no new `claimed_fixed_at`) |
| `open` | n/a | Token does not exist yet: 404 |
| `reopened` or any status after rotation | old (superseded) | 410 `token_superseded` |
| any | unknown token | 404 |

## 4. The verification algorithm

### 4.1 Inputs and outputs

```
evaluate(pothole, events, passes, coverage_inputs, now, thresholds) -> Decision
```
- `pothole`: current status, `claimed_fixed_at`, location.
- `events`: all events merged into the pothole (id, captured_at, confidence, device_id, pass_id).
- `passes`: all passes with a crossing near the pothole after the claim (with samples).
- `Decision`: `{new_status | None, reason, evidence, history_rows[], side_effects[]}`.

`evaluate` is pure and **idempotent**: calling it twice on the same data returns the same decision, and the service layer must not write a second history row if the status did not change (`TC-VER-17`).

### 4.2 Order of checks

1. If status is not one of `claimed_fixed`, `unknown`, `verified`: return no decision (nothing to verify).
2. **Contradiction check (T7).** Let `C` = events with `captured_at > claimed_fixed_at` and `confidence ≥ reopen_min_event_confidence`. If `len(C) ≥ reopen_min_events` return `reopened` with evidence `{before_event_id, after_event_ids, claimed_fixed_at}`. Contradiction wins over any number of clean passes.
3. **Coverage check.** Compute `coverage(pass, pothole)` for every pass whose crossing time is after `claimed_fixed_at` (section 4.3). Mark a pass **not clean** if any event from the same device with `captured_at` inside the pass interval is merged into this pothole (reason `detection_in_pass`).
4. If the number of **distinct clean passes** (distinct `pass.id` when `require_distinct_passes`) ≥ `required_clean_passes` return `verified` (or keep `verified` if already).
5. Else if at least one crossing exists after the claim (clean or not) and status is `claimed_fixed` return `unknown`; if status is `unknown` or `verified` keep it (a `verified` pothole never drops back to `unknown`).
6. Else: no change (still `claimed_fixed`, waiting for drives).

### 4.3 Coverage of one pass for one pothole

Given pass samples sorted by time and pothole location `P`:

1. **Find crossings.** For each sample compute `d_i = distance(sample_i, P)` (geodesic, metres). A crossing is a local minimum of `d_i` with `d_k ≤ pass_quality.max_lateral_offset_m`. If a pass has several crossings (loops), evaluate each; the pass is clean if **at least one** crossing is clean (record the best crossing). If there is no crossing, **no coverage row is created** (the pass says nothing about this pothole).
2. **Approach window** for crossing `k`: the samples `i < k` (before closest approach) with `pass_quality.window_min_m ≤ d_i ≤ pass_quality.window_max_m` **and** `heading_diff(sample_i.heading, bearing(sample_i → P)) ≤ pass_quality.max_heading_diff_deg` (the camera was facing the pothole).
3. **Per-sample checks.** A sample is OK only if all hold (record the first failing check as the reason, in this order):
   1. `detector_active` is true (if `require_detector_active`) — reason `detector_inactive`
   2. `inference_fps ≥ min_inference_fps` — `low_inference_fps`
   3. `accuracy_m ≤ max_gps_accuracy_m` — `poor_gps`
   4. `speed_mps ≤ max_speed_mps` — `too_fast`
   5. `sharpness ≥ min_sharpness` — `blurred`
   6. `mean_luma ≥ min_mean_luma` — `too_dark`
   7. if `occlusion_check_enabled`: `occlusion_ratio` is not null and `≤ max_occlusion_ratio` — `occluded` (a null value while the check is enabled counts as `occlusion_unknown`)
4. **Crossing result.**
   - If the window has fewer than `min_window_samples` samples → not clean, `insufficient_window_samples`.
   - Else `ok_fraction = ok_samples / window_samples`. If `ok_fraction ≥ min_window_ok_fraction` → clean, reason `ok`. Otherwise not clean, reason = the most common failing check among the window samples (ties broken by the order above).
5. Store `crossing_at` (time of closest approach), `clean`, `reason`, `window_samples`, `ok_fraction` in `pass_coverage` (a cache; always recomputable).

Assumption to state in the UI: when `occlusion_check_enabled` is false, passes can verify without an occlusion check, and the pothole detail page must say "occlusion not checked".

### 4.4 Reason codes (complete list)

`status_history.reason`: `first_event`, `report_sent`, `fixed_claim`, `clean_passes_met`, `no_valid_clean_pass`, `contradicting_event`, `reopen_report_sent`, `reminder_sent`, `escalation_sent`.
`pass_coverage.reason`: `ok`, `detector_inactive`, `low_inference_fps`, `poor_gps`, `too_fast`, `blurred`, `too_dark`, `occluded`, `occlusion_unknown`, `insufficient_window_samples`, `detection_in_pass`.

## 5. Before/after evidence

- `before_event_id`: when the claim is received (T3), choose the event of this pothole with the highest `confidence × area_ratio` among events with `captured_at ≤ claimed_fixed_at`.
- `after_events`: events with `captured_at > claimed_fixed_at`, newest first.
- A reopened pothole shows both side by side. A verified pothole shows the before image and the list of clean passes (count, times, reasons `ok`). Never show an "after" image for a verified pothole (there is none); show the pass log instead.

## 6. Worked scenarios (use these as tests)

Setup for all: pothole P, authority notified, thresholds as in `config/thresholds.yaml`, `required_clean_passes = 2`. `Pn` = pass n. Times increase downward.

| # | Sequence | Expected final status | Why |
|---|---|---|---|
| S1 | claim → P1 clean → P2 clean (different pass ids) | `verified` | T4 |
| S2 | claim → P1 clean | `claimed_fixed` | only 1 of 2 required passes; not decisive |
| S3 | claim → P1 blurred | `unknown` (T5) | pass exists, none clean |
| S4 | claim → P1 blurred → P2 clean → P3 clean | `verified` (via `unknown`) | T5 then T6; the blurred pass does not count against |
| S5 | claim → P1 clean → event at P (conf 0.8) | `reopened` | T7 beats clean passes |
| S6 | claim → P1 clean → P2 clean → verified → later event | `reopened` | T7 from `verified` |
| S7 | P1 clean at t1 → claim at t2 > t1 → P2 clean | `claimed_fixed` | P1 is before the claim and ignored |
| S8 | claim at t2; offline device uploads an event with `captured_at` < t2 after the claim | unchanged | pre-claim evidence; no reopen |
| S9 | one pass crosses P twice (a loop), both clean | counts as **1** clean pass | distinct pass ids |
| S10 | two passes with the same pass id uploaded twice | second is `duplicate`; counts once | idempotency |
| S11 | P2 uploaded before the event that explains it | same result as the other order | recompute, not increment |
| S12 | claim → P1 clean but the same device logged an event merged into P during P1 | P1 not clean, `detection_in_pass`; event reopens | consistency |
| S13 | claim when already `claimed_fixed` | unchanged, 200 | idempotent |
| S14 | reopen → new email → old Fixed link | 410 | token rotated |
| S15 | pass at lateral offset 12 m (other lane or street) | no coverage row | not a crossing |
| S16 | pass facing away (heading diff 120°) | window empty → not clean, `insufficient_window_samples` | camera not facing P |

## 7. Pseudocode (reference implementation guide)

```python
def evaluate(pothole, events, passes, now, th) -> Decision:
    if pothole.status not in {"claimed_fixed", "unknown", "verified"}:
        return Decision.none()

    contradicting = [e for e in events
                     if e.captured_at > pothole.claimed_fixed_at
                     and e.confidence >= th.verification.reopen_min_event_confidence]
    if len(contradicting) >= th.verification.reopen_min_events:
        return Decision.reopen(before=pothole.before_event_id,
                               after=[e.id for e in contradicting],
                               claimed_at=pothole.claimed_fixed_at)

    results = []
    for p in passes:
        r = coverage_for(p, pothole.location, events_in_pass=events_for(p), th=th.pass_quality)
        if r is not None and r.crossing_at > pothole.claimed_fixed_at:
            results.append(r)

    clean_ids = {r.pass_id for r in results if r.clean} if th.verification.require_distinct_passes \
                else [r for r in results if r.clean]
    if len(clean_ids) >= th.verification.required_clean_passes:
        return Decision.verify(clean_pass_ids=sorted(clean_ids), results=results)
    if results and pothole.status == "claimed_fixed":
        return Decision.unknown(results=results)
    return Decision.none()
```

## 8. Implementation notes

- Geodesic distance and bearing: use the haversine formula in `logic/geo.py` (pure Python, unit-tested against known values); the database `ST_DWithin` is for coarse candidate selection only (use a radius of `window_max_m + max_lateral_offset_m + 10`).
- The service layer, not `evaluate`, writes history rows and enforces "no duplicate row when nothing changed".
- Escalation and re-evaluation after `simulate-time` call the same functions with the simulated `now`.
- `pass_coverage` is a cache. If you change thresholds, delete and recompute it; do not migrate it.

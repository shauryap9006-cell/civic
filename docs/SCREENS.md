# SCREENS

Status: v0.1. Android (3 screens plus first-run consent) and website (map, detail, Fixed, impact, demo). States, copy and rules. Visual polish is secondary; clarity and honesty are not.

## 1. Shared rules

- **Status shown with colour AND icon AND label**, never colour alone.
- Severity always reads "graded band", never a physical measurement. Tooltip text: "Severity is a graded estimate from the image, not a measurement."
- Replay and seed data show a badge: **Replay** or **Seed**. Live data shows no badge (or **Live** in the demo page).
- Times display in the viewer's local time with the timezone abbreviation; the API sends UTC.
- No fabricated imagery: a verified pothole never shows an "after" image (there is none).

### Status tokens

| Status | Label | Icon (suggestion) | Colour (suggestion) |
|---|---|---|---|
| `open` | Detected | `!` in a circle | `#B5472B` terracotta |
| `reported` | Reported | envelope | `#D99A2B` amber |
| `claimed_fixed` | Claimed fixed | question mark in a wrench | `#8A8F3A` olive |
| `unknown` | Can't confirm | dashed circle | `#7A7468` warm grey |
| `verified` | Verified fixed | check mark | `#3F7D4E` green |
| `reopened` | Reopened | circular arrow | `#8C1F2D` deep red |

Tooltip texts: `claimed_fixed` = "The authority says it is fixed. Not yet confirmed." · `unknown` = "Later drives could not see this spot well enough to confirm the repair." · `verified` = "Later drives with good camera conditions saw no pothole here." · `reopened` = "A new detection contradicts the repair claim."

## 2. Android

### 2.1 First run: consent (blocks everything else)

- Title: "Help find and track potholes".
- Body: "While you drive, CivicLens looks at the road ahead on your phone. It saves small photos of the road surface (not the whole view), GPS points and camera-quality readings, and sends them to our server. It does not record audio and does not collect your name, phone number or contacts."
- Bullets: what is sent, what is not, "Photos can still show things on the road. Do not use this while driving unsafely." and "You can stop sharing and delete data on this phone at any time in Settings."
- Buttons: **I agree** (primary), **Not now** (exits to a plain explanation).
- Then permission requests in order: camera, location (precise), notifications. Each preceded by one sentence on why. If denied: explain what will not work and offer a way to open system settings.

### 2.2 Drive screen (main)

Layout (portrait or landscape):
- **Top status chips:** `GPS ±8 m` (green if ≤ `max_gps_accuracy_m`, amber if worse, red if none) · `10 FPS` (amber below `target_fps`, red below `min_fps`) · `Queue 3` (reports waiting to send) · battery/thermal icon.
- **Camera preview** with the ROI outline and live boxes (only while stationary).
- **Bottom bar:** large **Start** / **Stop** button (disabled with the text "Stop the vehicle to start or stop" when speed ≥ 5 km/h); counters "Potholes this drive: 4".
- Hint text before the first start: "Mount the phone at the centre of the windscreen so the lane ahead is visible, and plug in a charger."

States:
| State | Display |
|---|---|
| Ready | Start enabled, preview on |
| Driving | After the vehicle moves: the screen dims to a minimal view: "Detecting · 4 found · queue 0". No touch targets that need attention |
| Paused (thermal) | "Phone is too hot. Detection paused. Quality checks will mark this stretch as not usable." Shown only when stationary; otherwise only a subtle icon |
| No GPS | Chip red; Start still allowed; a note "Reports need GPS. Events without location are not saved." |
| Offline | Queue chip shows count; text "Saved. Will send when online." |
| Stopped | Navigates to trip summary |

### 2.3 Trip summary

- Distance, duration, average FPS, maximum thermal status.
- "Potholes found: N · Reports sent: X · Waiting to send: Y".
- A list of this drive's potholes (thumbnail, severity band, status once synced).
- Buttons: **Done**, **Send now** (visible if the queue is non-empty and the phone is online).
- Honest footnote: "Quality readings from this drive are also sent. They help confirm whether repairs really happened."

### 2.4 Settings

- **Share data** toggle (off = stop uploading; detection can still run locally if on).
- **City** (MVP: a single option "Demo City").
- **Delete data on this phone** (confirmation dialog; does not delete server data, and says so).
- **About:** app version, model version, device id (short), backend URL (editable only in development builds).
- **Diagnostics** (development builds): last upload errors, outbox counts, thresholds file version.

## 3. Website

### 3.1 Map page `/`

- Full-width MapLibre map with OSM-based tiles and attribution; header with the project name and links: Map, Impact.
- Pins styled per status token; clustering for dense areas; clicking a pin opens a popup: thumbnail, status label with icon, severity band, "first seen", "last event", **View evidence**.
- Left panel (collapsible on mobile): filters by status (checkboxes with icons), counts per status, legend.
- Empty state: "No potholes reported yet. Start a drive with the CivicLens app or run the demo replay."
- Badge **Replay**/**Seed** on popup and pin where relevant.

### 3.2 Pothole detail `/pothole/[id]`

Sections in order:
1. **Header:** status chip (icon, label, tooltip), severity band, authority name, location (approximate address if available, else coordinates), badges.
2. **Evidence photos:**
   - Reopened: two columns, "Before (date)" and "After (date)" with captions; plus "Why reopened: a new detection was captured after the repair claim."
   - Open/Reported/Claimed/Unknown: the best crop and a strip of up to 5 other crops.
   - Verified: the before crop and the text "No pothole was seen in N later drives with good camera conditions." (no after image).
3. **Timeline** from `history`: icon, label, time, plain-language reason: `first_event` → "First detected"; `report_sent` → "Reported to <authority>"; `fixed_claim` → "Authority claimed it is fixed"; `clean_passes_met` → "Confirmed by N clean drives"; `no_valid_clean_pass` → "Later drives could not confirm the repair"; `contradicting_event` → "New detection after the repair claim"; `reopen_report_sent` → "Authority notified again"; `reminder_sent`, `escalation_sent` → "Reminder sent" / "Escalated".
4. **Passes after the claim** (table): date/time, clean yes/no with icon, reason in words (`blurred` → "Image too blurry", `too_dark` → "Too dark", `occluded` → "View blocked by vehicles", `detector_inactive` → "Detector was not running", `low_inference_fps` → "Detector too slow", `poor_gps` → "GPS not precise enough", `too_fast` → "Driving too fast to be reliable", `insufficient_window_samples` → "Not enough good views of the spot", `detection_in_pass` → "A detection was recorded", `ok` → "Clear view, no pothole"). A caption: "Required clean drives: 2".
5. If `occlusion_check_enabled` is false: note "Blocked views were not checked in this version."
6. **Footer note:** "Severity is a graded estimate, not a measurement. Locations come from phone GPS and can be off by several metres."

### 3.3 Authority Fixed page `/fixed/[token]`

States:
| State | Content |
|---|---|
| Ready (`reported`) | Photo, location, severity band, status, short text "A driver's camera detected this pothole. If it has been repaired, please confirm." Button **Mark as fixed**. Optional note field. Small text: "This marks a claim. CivicLens will check it with later drives." |
| After claim | "Thank you. Marked as claimed fixed on <date>. CivicLens will verify the repair using later drives. A claim alone is not treated as proof." |
| Already claimed / verified | Shows current status; no button |
| Superseded (410) | "This link is out of date because the pothole was reopened after new evidence. Please use the link in the latest email." |
| Not found (404) | "We couldn't find this link." |
No login. No other navigation.

### 3.4 Impact `/impact`

Large counters (counts first, then percentages in small text): potholes detected, reports sent, claimed fixed, verified, reopened, can't confirm; km covered. A short note: "Counts include demo and replay data when shown with a badge." If replay/seed data is included, show a toggle "Include replay and seed data".

### 3.5 Demo `/demo` (only when `DEMO_MODE=true`)

- Banner: "Demo mode. Time skipping and reset are simulations."
- Buttons: **+1 day**, **+14 days**, **+28 days** (call `simulate-time`), **Reset demo state**, **Refresh**.
- Panel: current simulated time and offset, service health (db, storage), last result of the simulate call (status changes, reports sent).
- Protect with the admin secret (entered once, kept in memory only).

### 3.6 Email (template summary)

Subject: `[CivicLens] <Severity> pothole near <place or lat,lon>`. Body: one short paragraph, the crop image link, severity band with the "graded estimate" sentence, a map link, a prominent **Mark as fixed** link, a footer: "This is a pilot message sent to a test inbox." Reopened variant adds the before/after links and "A later drive detected the pothole again after the repair claim."

## 4. Accessibility and responsiveness

- Contrast ratio at least 4.5:1 for text; icons have text alternatives; the map is keyboard-reachable through the filter list and a "list of potholes" table view as an alternative.
- Layouts work at 360 px width. Tap targets at least 44 px.

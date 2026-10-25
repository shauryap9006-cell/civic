# DEMO SCRIPT

Status: v0.1. Target length 3.5 minutes. Roles: **Presenter** (talks), **Operator** (clicks, drives the laptop and phone), **Backup** (watches the clock and the failure checklist). One person can cover two roles if the team is small.

## 1. Principle

Show one loop: **detect → email → Fixed click → verify or reopen**, and be explicit about what is real and what is simulated. Honest labelling is part of the pitch; judges reward it and punish the opposite.

## 2. Real versus simulated (say this out loud and show it on a slide)

| Beat | Real or simulated |
|---|---|
| Detection on video from our own drives | Real |
| Phone running the detector | Real (state the measured FPS and duration) |
| Email delivery | Real, to a **test inbox** |
| Authority clicking Fixed | Simulated (we click it) |
| Later clean pass after the claim | Replay (labelled **Replay** on screen) unless a real repair happened |
| 14-day escalation | Simulated with the skip-time button |
| Several vehicles | Simulated by replaying clips as separate devices |

## 3. Pre-demo state (run 30 minutes before, then again 5 minutes before)

1. `make up` and start backend and web; `make db-reset` then `make demo-reset`.
2. Clock offset is 0 (`/healthz`).
3. Authority seeded; `EMAIL_MODE=test`, `TEST_INBOX` open in a browser tab with the inbox visible.
4. Recorded drive video, GPX and the replay set are on the laptop **and** on a USB drive.
5. Phone charged, mount ready, on the demo Wi-Fi/hotspot; app installed with consent already accepted; model loaded.
6. Slides: problem slide, real-vs-simulated slide, numbers slide, roadmap slide.
7. Backup video of the full demo is open in a separate window.
8. Browser tabs in order: Map `/`, Inbox, `/demo`.

## 4. Timeline

| Time | Presenter says | Operator does | On screen |
|---|---|---|---|
| 0:00–0:30 | "Potholes get reported, tickets get closed, and nobody checks the road. CivicLens proves whether it was really fixed." Show one sourced statistic if available **[To verify]** | — | Problem slide |
| 0:30–1:15 | "A phone on the windscreen runs the detector on the device. After three consecutive frames it creates a signed event with a road-only photo." | Start the live phone demo (or `make replay`); point at the detection overlay and the counter; show measured FPS | Phone screen mirrored or replay window |
| 1:15–1:45 | "The server merges duplicates and emails the authority once, to our test inbox." | Switch to the inbox; open the email: photo, severity band, map link, Fixed link | Inbox |
| 1:45–2:15 | "Anyone can see it on the map and open the evidence." | Open the map; click the pin; show the detail timeline (Detected → Reported) | Map and detail page |
| 2:15–2:45 | "The authority says it's fixed. That is only a claim." | Click the Fixed link in the email; click **Mark as fixed**; refresh the detail page: status **Claimed fixed** | Fixed page, detail page |
| 2:45–3:05 | "Now later drives. A blurred pass or a pass behind a truck doesn't count. Two clear passes do." | Run the replay passes (blurred one first → **Can't confirm**; then two clean → **Verified fixed**); show the coverage table with reasons | `/demo` or terminal + detail page |
| 3:05–3:20 | "And if the pothole is still there, we catch it." | Replay a pass with a detection on a second pothole that was claimed fixed → **Reopened**; show Before/After and the "reopened" email | Detail page, inbox |
| 3:20–3:30 | "Everything queues offline and syncs when the network returns. Our numbers: …; roadmap: fleets first, then licensing to Android and Linux cameras." | (If time) toggle airplane mode before the demo and show the queue upload at the start, otherwise skip | Numbers slide, roadmap slide |

If asked about escalation: click **+14 days** on `/demo` and show the reminder email in the inbox.

## 5. Numbers to prepare for the numbers slide

Fill from `ml/results/`: E1 precision/recall, E2 false events per km, E3 sustained FPS and temperature over 15–20 minutes, E4 gated verification vs naive absence as counts (and which cases are simulated), E5 location error median and 90th percentile, E6 offline test. Show test-set sizes. If a number does not exist, write "not measured".

## 6. Likely questions and honest answers

| Question | Answer |
|---|---|
| Is this the first? | No. Others detect and report; one prior system has a basic repair check **[To verify]**. We gate verification by pass quality. |
| How do you know it is really fixed? | We don't trust the claim. A repair counts only after several later drives with good image quality saw no pothole. Blurred, dark, occluded or low-quality passes are marked "can't confirm". |
| How accurate is location? | Phone GPS error; measured median X m, 90th percentile Y m (E5). A better GNSS module is an upgrade. |
| Can it run on any dashcam? | On any Android or Linux camera we can install on. Closed dashcams and factory cameras need a vendor partnership. |
| Privacy? | Road-only crops, no wide frames, consent screen, retention limits; plates and faces checked (TC-PRIV-02). |
| What about false reports or abuse? | Signed events from registered devices; production would require multiple independent devices before reopening. |
| Did you send this to real authorities? | No. Test inbox only. |
| Where is the real repair data? | We had N real repairs (count). The rest of the verification evidence is a labelled replay set. |

## 7. Failure plan (Backup watches for these)

| Failure | Action |
|---|---|
| Phone overheats or app crashes | Switch to replay on the laptop; say so |
| No internet | Use the local stack and the local inbox tab (`EMAIL_PROVIDER=console` shows the email in the backend log/UI); show the offline queue as a feature |
| Email delayed | Show the email already in the inbox from the rehearsal run; say "delivery can lag" |
| Backend down | Play the backup video; keep talking over it |
| Map tiles missing | Use the detail page and the list view |
| Anything unexpected | Never improvise a claim; say "that part is simulated" or "not measured" |

## 8. Rehearsal checklist

- Run the full script twice in a row from `make demo-reset` with a stopwatch.
- Practise the real-vs-simulated slide wording.
- Record the backup video on the final build; store it on two devices.
- Check the test inbox is not filtering the email as spam.
- Confirm no secrets appear on screen (`.env`, admin secret).

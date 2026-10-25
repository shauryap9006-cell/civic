# OPEN QUESTIONS

Status: living log. Humans answer; agents append. Format per entry: ID, date, raised by, question or conflict, what was assumed meanwhile, answer (when known). Move answered items to the bottom section.

## Open

| ID | Date | Raised by | Question | Assumption used meanwhile | Blocks |
|---|---|---|---|---|---|
| Q-01 | 2026-10-25 | team | How many hours do we have and how many people are on the team? | 3 people, enough time for gates G1–G5; G6 if time allows | Scope of Wow tier |
| Q-02 | 2026-10-25 | team | Which city and which authority contact do we configure? | "Demo City" with a placeholder authority; all email goes to the test inbox | `db/seed.sql`, email text |
| Q-03 | 2026-10-25 | team | Which Android phone is the test device and what is the minimum Android version? | A recent mid-range phone; minimum chosen after the first FPS test | T-50, T-51 |
| Q-04 | 2026-10-25 | team | Which detector licence route (D-08)? | Ultralytics YOLO for the hackathon only | Any commercial pitch |
| Q-05 | 2026-10-25 | team | Which public datasets, and are their licences compatible with our use? | None chosen yet | T-30 |
| Q-06 | 2026-10-25 | team | Retention periods for crops and raw pass samples | Keep until the end of the submission period; no automatic deletion yet | Privacy note |
| Q-07 | 2026-10-25 | team | Do we need real repair examples or is a labelled replay set enough for the submission? | Replay set, clearly labelled, plus real repairs if any occur | T-60, E4 |
| Q-08 | 2026-10-25 | team | Is `occlusion_check_enabled` true (vehicle labels) or false for the MVP? | true if vehicle pseudo-labels work within a day; else false with the UI note | T-30, T-33 |
| Q-09 | 2026-10-25 | team | Is the event in an idea/deck round first, with the build only after shortlisting? | Build the vertical slice anyway, prioritising G2 and a 60-second proof video | Schedule |

## Answered

| ID | Date | Question | Answer | Recorded in |
|---|---|---|---|---|
| — | — | — | — | — |

## How agents use this file

When two documents conflict, a requirement is ambiguous, or a threshold looks wrong: append a row under **Open** with the conflict, the interpretation you implemented, and the files affected. Do not wait for an answer; choose the safest interpretation (the one that is more conservative about declaring a repair verified) and keep going.

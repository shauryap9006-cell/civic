# CivicLens

A phone-as-dashcam pothole reporter that also **verifies repairs**: a "Fixed" claim from an authority becomes *verified* only when later drives, with image quality good enough to see the road, saw no pothole. A later detection reopens the case with before/after evidence.

Status: hackathon MVP specification pack. Start with `AGENTS.md`.

## Pack contents

| File | Purpose |
|---|---|
| `AGENTS.md`, `CLAUDE.md` | Instructions for coding agents (read first) |
| `docs/PRD.md` | Requirements with IDs and priorities |
| `docs/ARCHITECTURE.md` | Components, boundaries, repo layout, technology |
| `docs/WORKFLOWS.md` | Step-by-step flows from drive to verification |
| `docs/STATE_MACHINE.md` | Statuses, transitions, verification algorithm, worked examples |
| `docs/FEATURES.md` | Per-feature behaviour, edge cases and acceptance tests |
| `docs/TASKS.md` | Ordered tickets with dependencies and gates |
| `docs/TEST_PLAN.md` | Test cases, fixtures, evaluation protocol |
| `docs/DATA_AND_MODEL.md` | Labelling guide, datasets, training, export, data collection |
| `docs/SCREENS.md` | Android and website screens, states and copy |
| `docs/DEMO_SCRIPT.md` | Timed demo, roles, backups |
| `docs/DECISIONS.md`, `docs/OPEN_QUESTIONS.md` | Decision log and open items |
| `openapi.yaml`, `db/schema.sql` | API and database contracts |
| `config/thresholds.yaml` | All tunable numbers |
| `.env.example`, `docker-compose.yml`, `Makefile` | Local setup |

## Quick start (once the code exists)

```bash
cp .env.example .env          # fill TEST_INBOX and RESEND_API_KEY (or set EMAIL_PROVIDER=console)
make up                       # PostGIS + MinIO
# backend
cd backend && pip install -e . && uvicorn app.main:app --reload --port 8000
# web
cd web && npm install && npm run dev
# replay a recorded drive as a device
make replay VIDEO=data/drive1.mp4 GPS=data/drive1.gpx
```

Demo mode (`DEMO_MODE=true`) enables `/demo` on the website, `POST /v1/admin/simulate-time` and `POST /v1/admin/reset`.

## Safety and honesty

- Emails go to a test inbox only.
- Replayed or seeded data is labelled as such in the UI.
- Severity is a graded band, never a measurement.
- Check licences (detector, datasets) before any commercial use. See `docs/DECISIONS.md`.

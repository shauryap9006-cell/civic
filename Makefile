# Command contract for humans and agents. Each target must keep working as the repo grows.
# Recipes use ">" instead of tabs (see .RECIPEPREFIX).
# Targets marked TODO point at code that does not exist yet; implement them in the task named in docs/TASKS.md.

.RECIPEPREFIX = >
.PHONY: up down db-reset test test-backend test-core test-web lint replay demo-reset export-thresholds

up:                       # start PostGIS and MinIO
> docker compose up -d

down:
> docker compose down

db-reset:                 # wipe the database volume and re-apply schema.sql + seed.sql
> docker compose down -v
> docker compose up -d

test: test-core test-backend test-web

test-core:                # TODO T-13..T-17: python package core/ (pytest)
> cd core && python -m pytest -q

test-backend:             # TODO T-20..T-32: FastAPI app (pytest, needs `make up`)
> cd backend && python -m pytest -q

test-web:                 # TODO T-40..T-45
> cd web && npm test --silent

lint:
> cd backend && ruff check . && cd ../core && ruff check . && cd ../web && npm run lint --silent

replay:                   # TODO T-16: replay a recorded drive through the core and upload as a replay device
> cd core && python -m civiclens_core.replay --video "$(VIDEO)" --gps "$(GPS)" --api "$${API_BASE_URL:-http://localhost:8000}"

demo-reset:               # TODO T-62: reset DB to the clean demo state via the admin endpoint
> curl -fsS -X POST -H "X-Admin-Secret: $${ADMIN_SECRET}" "$${API_BASE_URL:-http://localhost:8000}/v1/admin/reset"

export-thresholds:        # TODO T-52: generate the Android asset from config/thresholds.yaml
> python tools/export_thresholds.py config/thresholds.yaml android/app/src/main/assets/thresholds.json

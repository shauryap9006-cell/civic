# CLAUDE.md

@AGENTS.md

## Claude Code specifics

- Read `AGENTS.md` first (imported above), then only the docs the current ticket needs.
- Use plan mode for tickets that touch more than three files or any state-machine logic; write tests before implementation for the cases named in `docs/TEST_PLAN.md`.
- Run `make test` (or the narrower target) before saying a task is complete, and paste the real result.
- Do not modify `docs/STATE_MACHINE.md`, `openapi.yaml`, `db/schema.sql` or `config/thresholds.yaml` without stating the reason and recording it in `docs/DECISIONS.md`.

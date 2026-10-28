# ReviewForge Backend

The local API and task-engine package for ReviewForge. Start it with:

```powershell
python -m pip install -e ".[dev]"
reviewforge
```

For the current API-only MVP, copy the repository's config examples, configure a
reviewer provider and project, set `security.code_egress_acknowledged: true`, and
set the configured provider's API key environment variable before submitting a
read-only `dry_run` review.

Agent integrations can start `python backend/mcp/server.py` as an MCP stdio
server. Set `AIH_CONFIG_DIR` when the Hub uses a non-default config directory;
the server reads the local runtime token and never opens the Hub database.

## Review API

All review endpoints require the local Bearer token or authenticated session
cookie. The root README includes a PowerShell submission example.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/reviews` | Submit a task; returns HTTP 202 and its ID |
| GET | `/api/reviews` | List tasks with project/status filters and pagination |
| GET | `/api/reviews/{id}` | Poll task state, stop reason, usage, and verdict |
| GET | `/api/reviews/{id}/events` | Inspect ordered state transitions |
| GET | `/api/reviews/{id}/rounds` | Inspect persisted review findings |
| GET | `/api/reviews/{id}/model-calls` | Inspect recorded model usage and cost |
| POST | `/api/reviews/{id}/cancel` | Cancel an active task |
| POST | `/api/reviews/{id}/rollback` | Roll back the latest verified application |
| GET | `/api/reviews/{id}/report` | Read the Markdown result report |
| GET | `/api/reviews/{id}/findings` | Read result findings as JSON |
| GET | `/api/reviews/{id}/validation` | Read check results and bounded output |
| GET | `/api/reviews/{id}/diff` | Read a model-proposed patch, if present |
| GET | `/api/reviews/{id}/artifacts/result` | Download the result ZIP |

Tasks progress through `CREATED`, `BUILDING_CONTEXT`, `REVIEWING`, `FIXING`, and
`VALIDATING`, then finish as `COMPLETED`, `NEEDS_ATTENTION`, or `CANCELLED`.
Only a passed quality gate produces `COMPLETED`. By default P0/P1 findings block
completion, tests must pass, and any configured acceptance check must pass.
Without an acceptance command the report marks display verification as absent.

## Current Boundaries

- `dry_run` defaults to `true`. With `dry_run: false`, a task uses the project's
  `finalize_mode`: `apply_to_project` applies the task diff with backups and
  verification, `branch` leaves the task branch for manual merge, and `dry_run`
  never touches the original project. When a fixer is configured, blocking
  findings or failed tests trigger bounded fixer attempts. Patches and complete-file
  edits are scope-checked, tested, committed at most once per round, and followed
  by a fresh review. Applying tasks can be rolled back while file hashes match.
- The default requires a clean project. If that check is disabled, review still
  uses committed files from HEAD; local edits and untracked files are not copied.
  Context selection uses tracked files, include/exclude patterns, and the selected
  scope. Automatic related-file expansion and context batching are not available.
- Top-level `limits` in `config.yaml` cap task round, token, cost, and running-time
  budgets. Project and submission limits can tighten these caps. Token reservations
  use a conservative byte estimate; missing provider usage is estimated and marked
  in validation results. Set actual model prices for meaningful cost enforcement.
- Review and fixer calls use the configured round, fix-attempt, token, cost, and
  wall-time limits. Automatic provider retries and invalid JSON repair are not
  implemented; an error or exhausted budget stops the task.
- Restarted tasks become `NEEDS_ATTENTION` with `interrupted`; the Hub does not
  repeat model calls automatically. A valid returned review and its usage remain
  recorded if validation is subsequently cancelled or interrupted. Result reports
  and ZIPs are available after validation completes; earlier failures can return
  HTTP 409 for these artifacts.
- Successful reviews remove their clean worktree. Worktrees and task branches
  that still exist after a failed or cancelled task are retained with contexts
  and artifacts for local inspection; automatic retention cleanup is not
  implemented. The branch created for a successful review also remains.

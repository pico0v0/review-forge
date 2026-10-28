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
| GET | `/api/reviews/{id}/report` | Read the Markdown result report |
| GET | `/api/reviews/{id}/findings` | Read result findings as JSON |
| GET | `/api/reviews/{id}/validation` | Read check results and bounded output |
| GET | `/api/reviews/{id}/diff` | Read a model-proposed patch, if present |
| GET | `/api/reviews/{id}/artifacts/result` | Download the result ZIP |

Tasks progress through `CREATED`, `BUILDING_CONTEXT`, `REVIEWING`, and
`VALIDATING`, then finish as `COMPLETED`, `NEEDS_ATTENTION`, or `CANCELLED`.
Only a passed quality gate produces `COMPLETED`. By default P0/P1 findings block
completion, tests must pass, and any configured acceptance check must pass.
Without an acceptance command the report marks display verification as absent.

## Current Boundaries

- Each task reviews the submitted Git commit once. `dry_run: false` is rejected;
  model proposals are archived without applying them. Repair loops and final
  apply/rollback are not available yet, even when `max_rounds` exceeds one.
- The default requires a clean project. If that check is disabled, review still
  uses committed files from HEAD; local edits and untracked files are not copied.
  Context selection uses tracked files, include/exclude patterns, and the selected
  scope. Automatic related-file expansion and context batching are not available.
- Top-level `limits` in `config.yaml` cap task round, token, cost, and running-time
  budgets. Project and submission limits can tighten these caps. Token reservations
  use a conservative byte estimate; missing provider usage is estimated and marked
  in validation results. Set actual model prices for meaningful cost enforcement.
- Review calls currently make one request; automatic provider retries and invalid
  JSON repair are not implemented. An error or exhausted budget stops the task.
- Restarted tasks become `NEEDS_ATTENTION` with `interrupted`; the Hub does not
  repeat model calls automatically. A valid returned review and its usage remain
  recorded if validation is subsequently cancelled or interrupted. Result reports
  and ZIPs are available after validation completes; earlier failures can return
  HTTP 409 for these artifacts.
- Successful reviews remove their clean worktree. Worktrees and task branches
  that still exist after a failed or cancelled task are retained with contexts
  and artifacts for local inspection; automatic retention cleanup is not
  implemented. The branch created for a successful review also remains.

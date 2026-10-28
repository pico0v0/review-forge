# ReviewForge

Automated AI code review and repair for local Git projects.

ReviewForge is being built to review code, repair it in isolated Git worktrees,
run project checks, and review again using configurable OpenAI-compatible models.
Its planned final apply step only accepts changes that pass the quality gate.

## Project Status

The backend currently supports local configuration, Git project registration,
bounded model connection checks, and a dry-run review/repair loop. Review tasks
run in isolated worktrees, record reviewer and fixer model usage, apply validated
patches or file edits only inside the task worktree, run configured tests and
acceptance checks, then re-review until the gate passes or a bounded stop is
reached. Final apply/rollback to the original project, MCP, and the web console
The repository also includes an MCP stdio client for Agent integrations;
the web console is still to come. See
[the development specification](docs/开发文档.md) for the planned architecture,
protocols, quality gates, and milestones. The V1 document is retained as history.

## Quick Start

Requires Python 3.11+ and Git. From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e "backend[dev]"
Copy-Item config/config.example.yaml config/config.yaml
Copy-Item config/projects.example.yaml config/projects.yaml
.\.venv\Scripts\reviewforge.exe
```

The Hub listens on `http://127.0.0.1:8787`. Configuration is read from `config/`
by default; use `reviewforge --config-dir <directory>` to choose another directory.
`AIH_` environment variables override YAML, for example `AIH_SERVER__PORT=8788`.
The default data directory is `~/.reviewforge/data`.

`GET /api/health` is public. Protected endpoints require the local token from
`<data_dir>/runtime/http_token` as `Authorization: Bearer <token>`. A successful
authenticated `GET /api/session` establishes an HttpOnly, SameSite=Strict cookie.
The token rotates when the Hub starts. Interactive API docs are available at
`http://127.0.0.1:8787/docs`.

Configure an MCP client with `python backend/mcp/server.py` after starting the
Hub. The server exposes submit, status, wait, result, history, cancel, models,
and finding-instruction tools and only communicates with the Hub over HTTP.

Before submitting code, configure a provider in `config/config.yaml`, set its key
environment variable, and explicitly acknowledge code egress:

```yaml
security:
  code_egress_acknowledged: true
```

The API defaults to read-only `dry_run` reviews. Set `dry_run: false` to enable
the project's finalization mode after the isolated gate passes. A review requires an
enabled Git project, a configured reviewer model, and a reviewer prompt stored
inside the Hub directory. Register a project and start a full-project review with
the authenticated API:

```powershell
$base = 'http://127.0.0.1:8787'
$token = (Get-Content "$env:USERPROFILE/.reviewforge/data/runtime/http_token" -Raw).Trim()
$headers = @{ Authorization = "Bearer $token" }
$projectRoot = (Resolve-Path 'C:/work/my-project').Path

$project = @{
  id = 'my-project'
  name = 'My Project'
  root = $projectRoot
  review_prompt = 'prompts/reviewer.md'
  test_command = 'python -m pytest'
}
Invoke-RestMethod -Method Post -Uri "$base/api/projects" -Headers $headers `
  -ContentType 'application/json' -Body ($project | ConvertTo-Json -Depth 10)

$request = @{ project_id = 'my-project'; scope = @{ mode = 'all' }; dry_run = $true }
$task = Invoke-RestMethod -Method Post -Uri "$base/api/reviews" -Headers $headers `
  -ContentType 'application/json' -Body ($request | ConvertTo-Json -Depth 10)
do {
  Start-Sleep -Seconds 2
  $task = Invoke-RestMethod -Uri "$base/api/reviews/$($task.id)" -Headers $headers
} while ($task.status -in @('CREATED', 'BUILDING_CONTEXT', 'REVIEWING', 'FIXING', 'VALIDATING', 'FINALIZING'))
Invoke-WebRequest -Uri "$base/api/reviews/$($task.id)/report" -Headers $headers `
  -OutFile "$($task.id)-report.md"
```

The submission response returns a task ID immediately. Poll
`GET /api/reviews/{task_id}` for status, then retrieve the report, findings,
validation results, and result archive from that task's endpoints. `scope.mode`
can be `all`, `paths`, `module`, or `diff`; diff reviews need a project
`default_base_ref` or an explicit `scope.base_ref`. The default diff base is
`HEAD`, which has no changes in the default clean-worktree mode. Configured test
and acceptance commands run in the isolated review worktree. Applying tasks also
run acceptance against the real project; failed final verification restores saved
snapshots. Use `POST /api/reviews/{task_id}/rollback` to roll back the latest
successful application when affected files have not changed.

## Backend Checks

```powershell
.\.venv\Scripts\python.exe -m ruff check backend/app backend/tests
.\.venv\Scripts\python.exe -m ruff format --check backend/app backend/tests
cd backend
..\.venv\Scripts\python.exe -m pytest -q
```

## Intended Stack

- Python 3.11+, FastAPI, SQLAlchemy, SQLite
- Vue 3, TypeScript, Vite
- OpenAI-compatible HTTP model providers
- Git worktrees, configurable test and acceptance commands
- HTTP API and an MCP stdio client for development agents

## Development Principles

- Validate and repair inside a separate worktree.
- Persist task states, rounds, model usage, and stop reasons.
- Enforce round, token, cost, and time limits.
- Keep credentials outside tracked configuration.
- Never push reviewed projects or execute commands suggested by a model.

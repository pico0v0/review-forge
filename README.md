# ReviewForge

Automated AI code review and repair for local Git projects.

ReviewForge builds review context, calls configurable OpenAI-compatible models,
repairs code in an isolated Git worktree, runs project checks, and reviews again.
Only changes that pass the quality gate can be applied to the original project.

## Project Status

The backend foundation is runnable: validated YAML/environment configuration,
SQLite migrations, a single-instance lock, local session authentication, and a
database health endpoint. Authenticated endpoints manage Git project registrations
and perform bounded OpenAI-compatible model connection checks. Review automation
and the console are being implemented.
See
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
The token rotates when the Hub starts.

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

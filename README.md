# ReviewForge

Automated AI code review and repair for local Git projects.

ReviewForge builds review context, calls configurable OpenAI-compatible models,
repairs code in an isolated Git worktree, runs project checks, and reviews again.
Only changes that pass the quality gate can be applied to the original project.

## Project Status

Implementation is starting from the V2 design. See
[the development specification](docs/开发文档.md) for the planned architecture,
protocols, quality gates, and milestones. The V1 document is retained as history.

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

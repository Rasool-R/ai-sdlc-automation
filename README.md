# AI Software Development Lifecycle Automation

A controlled, locally deployable AI software-engineering platform designed to analyze Python repositories, plan bounded changes, implement validated patches, execute tests and static analysis inside an isolated sandbox, perform structured code review, and prepare GitHub Pull Requests with mandatory human approvals.

## Core Safety Invariants

1. **Immutable Version Binding:** Every workflow and patch is bound to an immutable repository identity and base commit SHA.
2. **Untrusted Data Isolation:** Repository files, issues, and comments are treated strictly as untrusted data, never executable instructions.
3. **Privileged Boundary Separation:** LLMs never receive raw credentials, shell access, or direct container/GitHub privileges.
4. **Isolated Execution:** Untrusted repository and generated code runs exclusively inside the isolated `sandbox-runner` service.
5. **Mandatory Human Approvals:** Explicit human sign-off is required for implementation plans and before any branch/commit/PR submission. The platform contains no merge or deployment code paths.
6. **Secret Redaction & Privacy:** Secrets are redacted from logs, traces, and external payloads; private repository code is never transmitted externally without explicit opt-in.

## Architecture Overview

- **Backend API:** Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic
- **Workflow & Orchestration:** Deterministic LangGraph `StateGraph` with PostgreSQL checkpointer, executed via Celery & Redis
- **Repository Intelligence:** Python AST parser, PostgreSQL structural/dependency index, Qdrant semantic vector index
- **Execution & Quality Gates:** Isolated Docker `sandbox-runner`, Pytest, Ruff, Mypy, Bandit, `pip-audit`
- **Frontend:** Next.js, React, TypeScript, Tailwind CSS

## Quick Start (Backend Development)

```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
pytest
```
See `docs/architecture/overview.md` for detailed system architecture and trust boundaries.
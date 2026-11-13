# System Architecture and Trust Boundaries

## 1. High-Level Component Topology

```text
Next.js Frontend
  -> FastAPI Backend (Authentication, Projects, Repositories, Workflows, Approvals, SSE)
  -> Celery Workers + Redis Broker
  -> LangGraph StateGraph (Synchronous execution with PostgreSQL Checkpointer)
  -> Controlled Backend Services:
       - Repository Intelligence & Hybrid Retrieval (PostgreSQL + Qdrant)
       - Patch Validation & Test-Integrity Service
       - Sandbox Runner Service (Isolated container runtime boundary)
       - Static Analysis Service (Ruff, Mypy, Bandit, pip-audit)
       - GitHub Integration Service (GitHub App / Fine-Grained Token fallback)
       - Artifact Storage Service
```
## 2. Trust Boundaries
1. Operator / API Boundary: All workflow and administrative API routes require authenticated operator tokens.

2. LLM / Data Privacy Boundary: Outbound prompts pass through secret scanning and respect per-repository data-sharing policies before reaching any external LLM, embedding, or tracing provider.

3. Host / Sandbox Boundary: Neither the FastAPI service nor Celery workers mount the Docker socket. Only sandbox-runner manages ephemeral execution containers using a two-phase network policy (restricted egress during dependency installation; network=none during test execution).

4. GitHub Write Boundary: Branch creation, commits, and Pull Requests require validated base commit SHAs and cryptographically bound human approvals. Automatic merge and deployment capabilities do not exist in the codebase.
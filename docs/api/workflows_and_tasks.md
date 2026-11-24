# Workflows and Tasks API

All endpoints require a valid single-operator Bearer JWT obtained from `POST /api/v1/auth/login`.

## Workflows

| Method | Path | Description | Status Codes |
|---|---|---|---|
| `POST` | `/api/v1/workflows` | Create a version-pinned workflow run and enqueue background Celery execution | `201`, `401`, `404`, `409`, `422` |
| `GET` | `/api/v1/workflows` | List workflows (`repository_id`, `status`, `limit`, `offset`) | `200`, `401`, `422` |
| `GET` | `/api/v1/workflows/{workflow_id}` | Get workflow projection, persisted approvals, and aggregated LLM cost/usage | `200`, `401`, `404` |
| `POST` | `/api/v1/workflows/{workflow_id}/approve` | Submit hash-bound plan or commit/PR approval and enqueue resumption | `202`, `401`, `404`, `409`, `422` |
| `POST` | `/api/v1/workflows/{workflow_id}/reject` | Reject a paused workflow and enqueue termination | `202`, `401`, `404`, `409` |
| `POST` | `/api/v1/workflows/{workflow_id}/resume` | Supply clarification answers and enqueue resumption | `202`, `401`, `404`, `409`, `422` |
| `POST` | `/api/v1/workflows/{workflow_id}/cancel` | Cancel an active/paused workflow and enqueue checkpoint/workspace cleanup | `202`, `401`, `404`, `409` |

## Tasks

| Method | Path | Description | Status Codes |
|---|---|---|---|
| `GET` | `/api/v1/workflows/{workflow_id}/tasks` | List implementation tasks and dependency edges for a workflow | `200`, `401`, `404` |
| `GET` | `/api/v1/tasks/{task_id}` | Get an individual persisted task with resolved dependencies | `200`, `401`, `404` |

## Execution, Retry Ownership, and Safety Guarantees

- **Asynchronous execution:** FastAPI handlers never execute LangGraph synchronously; they persist the initial projection and dispatch jobs to Celery workers via Redis.
- **Retry ownership:** Celery retries only transient infrastructure failures (`OperationalError`, `InterfaceError`, `ConnectionError`, `TimeoutError`, `OSError`) with exponential backoff and jitter. LLM, schema-repair, and correction-loop retries live inside agent/graph nodes under explicit workflow budgets.
- **Non-blocking pauses:** At every human interrupt, LangGraph persists a PostgreSQL checkpoint and the Celery task exits immediately, freeing the worker. Approval/resume enqueues a new task targeting the same `thread_id`.
- **Mandatory cost limit:** `POST /api/v1/workflows` rejects any request without a positive `cost_limit_usd`.
- **Idempotency & approval binding:** Re-submitting an existing `idempotency_key` returns the existing workflow without enqueuing a duplicate job. Approvals require matching `approved_base_commit_sha` plus `approved_plan_hash` (for plan approval) or `approved_patch_hash` (for commit/PR approval).
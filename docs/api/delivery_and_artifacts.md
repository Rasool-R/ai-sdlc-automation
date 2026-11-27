# Pull Requests, Artifacts, and Reports API

All endpoints require a valid single-operator Bearer JWT obtained from `POST /api/v1/auth/login`.

## Pull Requests

| Method | Path | Description | Status Codes |
|---|---|---|---|
| `GET` | `/api/v1/pull-requests` | List persisted Pull Requests (`workflow_id`, `repository_id`, `state`, `limit`, `offset`) | `200`, `401`, `422` |
| `GET` | `/api/v1/pull-requests/{pull_request_id}` | Get Pull Request metadata; pass `?refresh=true` to query live GitHub CI and review status | `200`, `401`, `404`, `502` |

## Artifacts and Reports

| Method | Path | Description | Status Codes |
|---|---|---|---|
| `GET` | `/api/v1/workflows/{workflow_id}/artifacts` | List all versioned artifacts recorded for a workflow | `200`, `401`, `404` |
| `GET` | `/api/v1/artifacts/{artifact_id}` | Return authorized artifact metadata, or stream raw bytes with `?download=true` | `200`, `401`, `403`, `404`, `409` |
| `GET` | `/api/v1/workflows/{workflow_id}/report` | Return the evidence-based completion report combining changes, tests, static analysis, review, approvals, usage, PR, and final report | `200`, `401`, `403`, `404`, `409` |

## Safety and Integrity Guarantees

- **No merge or deployment paths:** Pull Requests are opened for human review only; merge and deployment occur outside the platform.
- **Idempotent delivery:** Branch push and PR creation use a deterministic `operation_id` bound to `(workflow_id, base_commit_sha, patch_hash)` and refuse to push if the remote base branch advanced.
- **Artifact integrity and authorization:** Every artifact is secret-redacted before storage, checksummed with SHA-256, verified on read (`409 Conflict` if corrupted on disk), restricted to its authorized owner (`403 Forbidden`), and tagged with an expiration timestamp for retention cleanup.
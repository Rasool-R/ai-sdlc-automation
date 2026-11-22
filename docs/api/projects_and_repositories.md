# Projects and Repositories API

All endpoints below require a valid single-operator Bearer JWT obtained from `POST /api/v1/auth/login`.

## Projects

| Method | Path | Description | Status Codes |
|---|---|---|---|
| `POST` | `/api/v1/projects` | Create a new project | `201`, `401`, `409`, `422` |
| `GET` | `/api/v1/projects` | List projects (`limit`, `offset`) | `200`, `401`, `422` |
| `GET` | `/api/v1/projects/{project_id}` | Get project details | `200`, `401`, `404` |
| `DELETE` | `/api/v1/projects/{project_id}` | Delete a project and cascaded records | `204`, `401`, `404` |

## Repositories

| Method | Path | Description | Status Codes |
|---|---|---|---|
| `POST` | `/api/v1/repositories/connect` | Authorize via GitHub, persist repo, and pin base commit SHA | `201`, `401`, `403`, `404`, `409`, `422`, `502` |
| `GET` | `/api/v1/repositories` | List repositories (`project_id`, `limit`, `offset`) | `200`, `401`, `422` |
| `GET` | `/api/v1/repositories/{repository_id}` | Get repository metadata and latest pinned version | `200`, `401`, `404` |
| `POST` | `/api/v1/repositories/{repository_id}/index` | Acquire lock, clone isolated workspace, and incrementally index AST + optional Qdrant embeddings | `200`, `401`, `404`, `409`, `422`, `502` |
| `GET` | `/api/v1/repositories/{repository_id}/structure` | Query indexed Python symbols and dependency edges (`commit_sha`, `file_path`, `kind`, `limit`, `offset`) | `200`, `401`, `404`, `422` |

### Privacy, Fallback, and Safety Defaults

- `llm_data_sharing` defaults to `false` on every connected repository; private code is never transmitted to an external LLM or embedding provider without explicit opt-in and prior secret scanning.
- Local/self-hosted embedding endpoints (`EMBEDDING_PROVIDER=local` with `EMBEDDING_BASE_URL`) are permitted for private repositories without external data-sharing opt-in.
- If Qdrant or the embedding provider is unavailable or denied by policy, indexing and retrieval degrade gracefully to structural-only PostgreSQL mode without failing.
- Every indexed snapshot, symbol, relationship, and vector is strictly scoped to `(repository_id, commit_sha)` so versions are never mixed.
- Credentials are never returned in API responses.
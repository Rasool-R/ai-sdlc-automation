# Demo Catalog API

Minimal FastAPI service used as a disposable reference repository for automated SDLC workflows.

## Endpoints

- `GET /health` — Service health probe returning `{"status": "ok"}`.
- `GET /items` — Lists all catalog items along with the total count.
- `GET /items/{item_id}` — Retrieves a single catalog item by ID.

## Running Tests

```bash
pytest
```
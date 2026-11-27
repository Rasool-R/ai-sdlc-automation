# Workflow Events and Server-Sent Events (SSE) API

All requests require a valid single-operator Bearer JWT obtained from `POST /api/v1/auth/login`.

## Endpoint

`GET /api/v1/workflows/{workflow_id}/events`

### Query Parameters and Headers

| Name | Location | Default | Description |
|---|---|---|---|
| `after_id` | Query | `None` | Return only events strictly newer than this monotonic event ID |
| `limit` | Query | `100` | Maximum events returned per batch (`1..500`) |
| `stream` | Query | `false` | Set `true` to stream live Server-Sent Events (`text/event-stream`) |
| `follow` | Query | `true` | In SSE mode, keep polling until workflow reaches a terminal status or `max_duration_seconds` elapses; set `false` for one-shot replay |
| `poll_interval_seconds` | Query | `0.5` | Polling interval in SSE mode (`0.05..10.0`) |
| `max_duration_seconds` | Query | `30.0` | Upper bound for a single SSE connection (`0.1..300.0`) before client reconnects |
| `Accept` | Header | `application/json` | Sending `text/event-stream` also enables SSE streaming |
| `Last-Event-ID` | Header | `None` | Fallback monotonic cursor when `after_id` is omitted |

### Modes

1. **Cursor-based JSON list (default):**
   Returns `items` ordered strictly by monotonic `id`, `total`, `next_cursor`, and `has_more`.
2. **Server-Sent Events (`stream=true` or `Accept: text/event-stream`):**
   Streams standard SSE frames (`id`, `event`, `data`) with `Cache-Control: no-cache` and `X-Accel-Buffering: no`. Closes cleanly once a terminal workflow status (`completed`, `failed`, `rejected`, `cancelled`) has been flushed.

### Security and Privacy Guarantees

- Unauthenticated requests receive `401 Unauthorized`; unknown workflow IDs receive `404 Not Found` before starting any stream.
- All event messages and JSON payloads pass through secret redaction and strip restricted internal reasoning keys before persistence and transmission.
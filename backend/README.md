# Backend — llms.txt Generator

FastAPI backend that crawls a website and generates a spec-compliant `llms.txt`,
with a WebSocket that streams the crawl's progress live. Runs on ECS Fargate in
production; see `../terraform/DEPLOYMENT_GUIDE.md` for deployment.

## Setup

```bash
cd backend
pip install -r requirements-dev.txt   # runtime deps + pytest (use requirements.txt to run only)
```

The app reads configuration from environment variables (there is no `.env`
auto-loading). It runs with **no configuration** — generation works out of the box;
the variables below enable optional features:

| Variable | Enables | Default |
|---|---|---|
| `DATABASE_URL` | Postgres persistence + change detection | off (generation still works) |
| `OPENROUTER_API_KEY` | AI-enhanced descriptions (`enhance`) | off |
| `OPENROUTER_MODEL` / `OPENROUTER_BASE_URL` | override the LLM model / endpoint | `openai/gpt-4o-mini` / OpenRouter |
| `BRIGHT_DATA_CDP_URL` | browser rendering for blocked/JS sites (`bypass`) | off |
| `S3_BUCKET` / `S3_REGION` | uploading the generated file to S3 | off / `us-east-2` |
| `FRONTEND_ORIGIN` | CORS + WebSocket origin allowlist (comma-separated) | `*` |
| `CRON_SECRET` | shared secret for the nightly refresh endpoint | off (endpoint fail-shut) |
| `MAX_CONCURRENT_GENERATIONS` | in-flight generation cap | `8` |

## Run

```bash
uvicorn app:app --reload
```

Serves on http://localhost:8000. Health check:

```bash
curl http://localhost:8000/health   # {"status":"ok"}
```

## Test

```bash
pytest
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/generate` | Generate an llms.txt (blocking, returns the result) |
| `WS` | `/ws/generate` | Generate with live progress streaming |
| `POST` | `/check-changes` | Re-crawl a stored site and report if it changed |
| `POST` | `/internal/cron/refresh` | Trigger the nightly auto-update sweep (needs `X-Cron-Secret`) |
| `GET` | `/health` | Liveness probe |

Both `/generate` and `/ws/generate` accept the same JSON body:

```json
{
  "url": "https://example.com",
  "max_pages": 50,
  "crawl": true,
  "enhance": false,
  "bypass": false,
  "honor_robots": true,
  "auto_update": false,
  "recrawl_interval_days": 1
}
```

`POST /generate` returns:

```json
{
  "llms_txt": "# Example\n\n> ...",
  "pages": [
    { "url": "...", "title": "...", "description": "...",
      "lastmod": null, "priority": null, "md_url": null }
  ],
  "warnings": [],
  "public_url": "https://<bucket>.s3.<region>.amazonaws.com/example.com.txt"
}
```

## WebSocket Endpoint

Connect to `ws://localhost:8000/ws/generate` and send the JSON body above as the
first message. The server streams frames until a terminal one, then closes:

```jsonc
// progress events, many of these
{"type": "event", "stage": "fetch", "phase": "done", "url": "https://example.com/", "status": 200, "ok": true, "done": 3, "total": 12}

// terminal — exactly one of:
{"type": "done", "result": { "llms_txt": "...", "pages": [], "warnings": [], "public_url": null }}
{"type": "error", "detail": "Could not generate llms.txt: ..."}
```

`result` in the `done` frame is identical to `POST /generate`'s response body. Event
frames carry `stage`, `phase` (`start`/`done`/`info`), and — depending on the
stage — `url`, `status`, `ok`, `done`/`total`, `message`, and `browser`.

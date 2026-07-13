# llms.txt Generator

Point it at a website and it crawls the site, reads each page, and writes a
spec-compliant `llms.txt` — the Markdown index that tells language models what a
site contains and where its important pages are. Generation streams progress over
a WebSocket, results are stored on S3, and an optional nightly job keeps each file
in sync as the source site changes.

Live app: https://llmstextgeneratorrishishah.com
API: https://api.llmstextgeneratorrishishah.com — [`/health`](https://api.llmstextgeneratorrishishah.com/health), [`/docs`](https://api.llmstextgeneratorrishishah.com/docs)

---

## What it does

Given a URL, the backend discovers the site's pages (sitemaps first, then a
breadth-first crawl), extracts a title and description for each, groups them into
sections, and renders a single `llms.txt` file. A validator checks the output
against the [llmstxt.org](https://llmstxt.org/) format before it is returned.

Notable behavior:

- Discovery reads `robots.txt` and sitemaps, then falls back to crawling links —
  with a configurable depth and page cap.
- Pages that only render with JavaScript, or that sit behind a bot wall, can be
  fetched through a real browser (Playwright driving Bright Data's Scraping
  Browser over CDP).
- Descriptions can optionally be rewritten by an LLM (OpenRouter,
  `openai/gpt-4o-mini` by default); without a key the heuristic output is used.
- Enabling auto-update enrolls a site in a nightly re-check that regenerates and
  re-uploads the file only when the content has actually changed.
- Every generation streams stage-by-stage progress to the browser and finishes by
  uploading the file to S3 with a public URL.

## How it works

A single request runs through an ordered pipeline, one module per stage:

```
robots → homepage → discover (sitemap + crawl) → fetch → extract
       → rank → format → validate → upload
```

`generator.py` orchestrates it; each stage lives in its own module (`discoverer`,
`crawler`, `fetcher`, `extractor`, `ranker`, `formatter`, `validator`, with
`curator` and `browser` handling the optional LLM and browser paths). Ordering is
deliberately stable so that a site's content hash — used for change detection —
only moves when the site itself changes.

The deployed system is two pieces:

- A **Next.js** frontend on Vercel that opens a WebSocket to the API and renders
  the live log plus the finished file.
- A **FastAPI** service on AWS ECS Fargate (ARM64/Graviton, 2–6 tasks behind an
  ALB with ACM TLS). It talks to Neon for metadata, S3 for the generated files,
  and Bright Data / OpenRouter for the optional paths. A nightly EventBridge
  schedule calls the service's refresh endpoint to run the auto-update sweep — no
  Lambda involved.

## Stack

- Backend: FastAPI on Python 3.12, httpx, BeautifulSoup4 + lxml, Playwright (CDP
  only), boto3, psycopg
- Frontend: Next.js 16 (App Router), TypeScript, hand-written CSS
- Data: Neon (serverless Postgres), Amazon S3 (public-read bucket)
- External: Bright Data Scraping Browser, OpenRouter
- Infrastructure: ECS Fargate, ECR, ALB + ACM, EventBridge, Route 53, Vercel, all
  provisioned with Terraform

## Local development

The backend runs with no configuration at all — generation works out of the box,
and the database, storage, and AI paths switch on only when their env vars are set.

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app:app --reload
```

There is no `playwright install` step (the browser path is remote over CDP) and no
database migration step (the schema is created on startup). The API serves on
`http://localhost:8000`, with Swagger at `/docs`.

Frontend:

```bash
cd frontend
cp .env.local.example .env.local     # NEXT_PUBLIC_API_URL points at the backend
npm install
npm run dev
```

## Configuration

The backend reads plain environment variables (there is no `.env` auto-loading;
`docker-compose.yml` injects them through `env_file`). All are optional.

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Neon Postgres connection string; enables persistence + change detection |
| `S3_BUCKET`, `S3_REGION` | Where generated files are uploaded (`S3_REGION` defaults to `us-east-2`) |
| `BRIGHT_DATA_CDP_URL` | CDP endpoint for browser rendering of blocked/JS pages |
| `OPENROUTER_API_KEY` | Enables AI-written descriptions |
| `OPENROUTER_MODEL` | Overrides the model (default `openai/gpt-4o-mini`) |
| `FRONTEND_ORIGIN` | Comma-separated allowlist for CORS and the WebSocket origin check |
| `CRON_SECRET` | Shared secret for the nightly refresh endpoint (unset = endpoint refuses everything) |
| `MAX_CONCURRENT_GENERATIONS` | Cap on in-flight generations (default 8) |

Generate the cron secret with `openssl rand -base64 32`.

## API reference

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/generate` | Generate a file (blocking) and return the result |
| WS | `/ws/generate` | Generate with live progress streaming |
| POST | `/check-changes` | Re-crawl a stored site and report whether it changed |
| POST | `/internal/cron/refresh` | Run the nightly auto-update sweep (needs `X-Cron-Secret`) |
| GET | `/health` | Liveness probe |

`POST /generate` and `/ws/generate` take the same body:

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

and produce:

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

Over the WebSocket, send that body as the first message. The server streams
progress frames and then exactly one terminal frame:

```jsonc
{"type": "event", "stage": "fetch", "phase": "done", "url": "...", "status": 200, "ok": true, "done": 3, "total": 12}
{"type": "done",  "result": { "llms_txt": "...", "pages": [], "warnings": [], "public_url": null }}
{"type": "error", "detail": "Could not generate llms.txt: ..."}
```

The full endpoint reference lives in [`backend/README.md`](./backend/README.md).

## Testing

```bash
cd backend
pytest
```

229 tests cover the pipeline modules, the persistence layer, and the REST +
WebSocket surface.

## Deployment

Everything infrastructure-related is in `terraform/`, and
[`terraform/DEPLOYMENT_GUIDE.md`](./terraform/DEPLOYMENT_GUIDE.md) walks through a
from-scratch deploy: AWS prerequisites, the ARM64 image build, ECR/ECS, DNS and
TLS, and monitoring. The image must be built for ARM64 to match Graviton.

## Security and operations

- The WebSocket validates the browser `Origin` against `FRONTEND_ORIGIN`, since
  WebSockets are not covered by CORS.
- REST endpoints use a configurable CORS allowlist and Pydantic request models.
- `/internal/cron/refresh` fails shut: with no `CRON_SECRET` set it rejects every
  request.
- A per-process semaphore caps concurrent (and potentially paid) generations.
- Traffic terminates TLS at the ALB via ACM.
- Application logs, including each nightly sweep, land in the CloudWatch log group
  `/ecs/llms-backend`; alarms for 5xx rate, unhealthy hosts, CPU, and memory
  publish to an SNS email topic.

## Project layout

```
backend/     FastAPI service and the generation pipeline (one module per stage)
  tests/     229-test suite
frontend/    Next.js app (App Router, plain CSS)
terraform/   ECS, ALB, ACM, S3, EventBridge, IAM + DEPLOYMENT_GUIDE.md
docker-compose.yml   local backend container
```

## Next Steps

1. **Accounts and saved libraries** — Add auth/login so each user signs in and
   sees the `llms.txt` files they've generated, turning today's stateless one-off
   flow into a personal library.
2. **Version history** — Keep every regeneration as a numbered version per site,
   so users can see when a file changed and diff what's new — building on the
   content-hash change detection that already exists.
3. **Interactive control over the WebSocket** — Use the socket's two-way channel
   for more than streaming progress: let users steer a running generation live —
   for example, raise the page limit or flip on browser-unblock after seeing pages
   get blocked in the log, or stop early and finalize with what's been collected —
   without restarting. That kind of mid-run, reactive control is something a plain
   request or a one-directional stream can't offer, which is what justifies keeping
   a persistent connection.
4. **On-demand and Lambda-based syncing** — Let users trigger a re-sync of a saved
   site at any time, not only on the nightly run, and move the scheduled refresh
   into a dedicated Lambda so the job is isolated from the request-serving service.
5. **Async generation at scale** — Move generation off the request path into a job
   queue with workers and cache recent results, so large "no limit" crawls don't
   hold a connection open and repeat URLs reuse prior work instead of re-crawling.

## License

Released under the MIT License — see [LICENSE](./LICENSE).

import asyncio
import os
from dataclasses import asdict

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

import db
import refresher
import storage
from generator import generate
from progress import Event, emit, noop
from urls import scope_of

app = FastAPI(title="llms.txt Generator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("FRONTEND_ORIGIN", "*").split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)


# The one admission cap: bounds concurrent generations per process, because each
# can open a PAID Bright Data session. Acquired by _run_generation and
# /check-changes (every paid-session entry point); the WS handler must NOT acquire
# it too, or the two acquisitions deadlock. Env-overridable for tuning and tests.
MAX_CONCURRENT_GENERATIONS = max(1, int(os.getenv("MAX_CONCURRENT_GENERATIONS", "8")))
_generation_slots = asyncio.Semaphore(MAX_CONCURRENT_GENERATIONS)

# Bounds the WS teardown wait. A cancelled crawl can take real time to unwind
# (e.g. browser.py's _cleanup); an unbounded wait would pin the handler -- and
# uvicorn's graceful shutdown -- forever. Leaking a task beats wedging the handler.
WS_CLEANUP_TIMEOUT_SECONDS = 30


def _origin_allowed(origin: str | None) -> bool:
    # WebSockets bypass CORS entirely, so CORSMiddleware gives /ws/generate zero
    # protection. This stops browser drive-by cross-origin use and nothing more --
    # Origin is browser-set and any non-browser client forges it freely.
    allowed = [o.strip() for o in os.getenv("FRONTEND_ORIGIN", "*").split(",")]
    if "*" in allowed:
        return True
    if origin is None:
        # TestClient and wscat send no Origin. CORS only ever gates browsers.
        return True
    return origin in allowed


@app.on_event("startup")
def _startup() -> None:
    # Persistence is best-effort -- the app still generates if the DB is absent.
    try:
        db.init_db()
    except Exception:
        pass


class GenerateRequest(BaseModel):
    url: str
    max_pages: int | None = 50
    crawl: bool = True
    enhance: bool = False
    bypass: bool = False
    honor_robots: bool = True
    # Opt-in nightly refresh: a midnight-UTC sweep re-checks the site every N days.
    auto_update: bool = False
    recrawl_interval_days: int = 1


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


def _store_file(site_url: str, document: str, warnings: list) -> tuple[str | None, str | None]:
    # Best-effort: a storage failure must not fail the generation.
    key = storage.object_key_for(site_url)
    try:
        url = storage.upload_llms_txt(document, key)
        return key, url
    except Exception as error:  # noqa: BLE001 - surface as a warning, never 500
        # `or [""]` guards an exception with an empty str() (splitlines() -> []).
        warnings.append(f"file storage skipped: {(str(error).splitlines() or [''])[0]}")
        return None, None


def _record(base_url, scope_prefix, result, settings, object_key, public_url) -> dict:
    return {
        "site_url": base_url,
        "scope_prefix": scope_prefix,
        "content_hash": db.site_hash(result["pages"]),
        "page_count": len(result["pages"]),
        "warnings": result["warnings"],
        "object_key": object_key,
        "public_url": public_url,
        **settings,
    }


def _persist(base_url, scope_prefix, result, settings, object_key, public_url) -> None:
    # Store generation metadata in Postgres. Best-effort: a failure only warns.
    try:
        db.save_generation(_record(base_url, scope_prefix, result, settings, object_key, public_url))
    except Exception as exc:
        result["warnings"].append(f"could not persist generation: {exc}")


@app.get("/health")
async def health() -> dict:
    # Async so it runs on the event loop and stays responsive even when the
    # sync-handler threadpool is saturated by long crawls.
    return {"status": "ok"}


async def _run_generation(request: GenerateRequest, on_event=noop) -> dict:
    """The whole generation sequence, shared by POST /generate and WS /ws/generate.

    Holds a _generation_slots slot, so the cap covers both entry points. The WS
    handler must NOT acquire it too (it would deadlock this one) -- it only
    pre-checks .locked() to refuse fast with 1013.

    Raises HTTPException. The WS handler MUST catch it: raised inside an accepted
    WebSocket it kills the connection instead of producing an error frame.
    """
    if not request.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="url must start with http:// or https://")
    max_pages = request.max_pages if (request.max_pages and request.max_pages > 0) else None
    settings = {
        "crawl": request.crawl, "max_pages": max_pages,
        "enhance": request.enhance, "bypass": request.bypass,
        "honor_robots": request.honor_robots,
    }
    base_url, scope_prefix = scope_of(request.url)

    async with _generation_slots:
        async with make_client() as client:
            result = await generate(
                request.url, client, max_pages, request.crawl, request.enhance, request.bypass,
                honor_robots=request.honor_robots, on_event=on_event,
            )

    # A zero-page result (site unreachable/blocked) is a silent failure, not a
    # success -- surface it instead of returning a degenerate file.
    if not result["pages"]:
        reason = "; ".join(result["warnings"]) or "no pages could be discovered"
        raise HTTPException(status_code=422, detail=f"Could not generate llms.txt: {reason}")

    emit(on_event, Event(stage="persist", phase="start", message="uploading to S3"))
    object_key, public_url = await run_in_threadpool(
        _store_file, base_url, result["llms_txt"], result["warnings"]
    )
    await run_in_threadpool(
        _persist, base_url, scope_prefix, result, settings, object_key, public_url
    )
    emit(on_event, Event(stage="persist", phase="done"))

    if request.auto_update:
        # Best-effort like all persistence; the floor keeps a zero/negative
        # interval from becoming "due every sweep forever".
        try:
            await run_in_threadpool(
                db.enroll_auto_update, base_url, max(1, request.recrawl_interval_days)
            )
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(f"auto-update enrollment failed: {exc}")

    result["public_url"] = public_url
    return result


@app.post("/generate")
async def generate_endpoint(request: GenerateRequest) -> dict:
    return await _run_generation(request)


@app.websocket("/ws/generate")
async def ws_generate(websocket: WebSocket) -> None:
    # accept() BEFORE any close(code). uvicorn discards the code on a pre-accept
    # close and sends a bare HTTP 403 instead, so the browser would see 1006 and
    # never 1008/1013. (TestClient fabricates the code and hides this.)
    await websocket.accept()

    if not _origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=1008)
        return

    try:
        request = GenerateRequest(**await websocket.receive_json())
    except WebSocketDisconnect:
        # Client is already gone -- nobody to send an error frame to.
        return
    except Exception as error:  # noqa: BLE001 - malformed first frame
        try:
            await websocket.send_json({"type": "error", "detail": f"bad request: {error}"})
            await websocket.close()
        except RuntimeError:
            pass  # socket died between the receive and the send
        return

    # Fast refusal only -- the slot is acquired inside job()'s _run_generation, not
    # here (acquiring it here too would deadlock). Kept right before the job with no
    # await in between, so the race is benign (real cap 8 or 9, never unbounded).
    if _generation_slots.locked():
        await websocket.close(code=1013)  # try again later
        return

    queue: asyncio.Queue = asyncio.Queue()

    def on_event(event: Event) -> None:
        # Sync by design: put_nowait can never await, so an emit cannot block a
        # fetch or yield control inside a gather.
        queue.put_nowait({"type": "event", **asdict(event)})

    async def writer() -> None:
        # The ONLY thing that ever sends on this socket. The terminal frame goes
        # through this same queue, so it can never overtake a pending event.
        while True:
            frame = await queue.get()
            await websocket.send_json(frame)
            if frame["type"] in ("done", "error"):
                return

    async def watch_disconnect() -> None:
        # receive() RETURNS the disconnect message, it does not raise, so loop
        # until we actually see one -- a stray client frame must not cancel a
        # healthy crawl. Started only after the params are read (receive() is not
        # multiplexed; a watcher racing that read would eat the params frame).
        while True:
            if (await websocket.receive())["type"] == "websocket.disconnect":
                return

    async def job() -> None:
        try:
            result = await _run_generation(request, on_event)
            queue.put_nowait({"type": "done", "result": jsonable_encoder(result)})
        except HTTPException as error:
            # Raising this inside an accepted websocket does NOT produce an error
            # frame -- it kills the connection. Translate it.
            queue.put_nowait({"type": "error", "detail": error.detail})
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - never die without a terminal frame
            detail = (str(error).splitlines() or [type(error).__name__])[0]
            queue.put_nowait({"type": "error", "detail": detail})

    tasks = [asyncio.create_task(coro())
             for coro in (job, writer, watch_disconnect)]
    _, writer_task, disconnect_task = tasks

    # Wait on the WRITER, not the job: the job finishes by PUTTING its terminal
    # frame on the queue, so waiting on it would cancel the writer with that
    # frame still unsent. The writer exits once it has sent it. The disconnect
    # watcher is here so a closed tab cancels the crawl; the writer is here so
    # a dead socket doesn't leave the crawl filling a queue nobody drains.
    await asyncio.wait({writer_task, disconnect_task},
                       return_when=asyncio.FIRST_COMPLETED)

    for task in tasks:
        if not task.done():
            task.cancel()
    # cancel() only requests cancellation; await the tasks so a crawl mid-teardown
    # (httpx client, PAID browser session) finishes unwinding. Bounded via
    # asyncio.wait (not wait_for, which would re-await a task that resists cancel
    # and hang here) so a wedged task can't pin the handler.
    done, _pending = await asyncio.wait(tasks, timeout=WS_CLEANUP_TIMEOUT_SECONDS)
    # Retrieve exceptions so asyncio doesn't log "never retrieved"; skip cancelled.
    for task in done:
        if not task.cancelled():
            task.exception()

    try:
        await websocket.close()
    except RuntimeError:
        pass  # already closed by the client


@app.post("/internal/cron/refresh")
def cron_refresh(background_tasks: BackgroundTasks,
                 x_cron_secret: str | None = Header(default=None)) -> dict:
    # Shared-secret gate; with no CRON_SECRET configured the endpoint stays
    # closed (fail-shut). Work runs after the response so the scheduler's HTTP
    # call returns immediately instead of holding a connection for whole crawls.
    secret = os.getenv("CRON_SECRET")
    if not secret or x_cron_secret != secret:
        raise HTTPException(status_code=401, detail="Unauthorized")
    # The semaphore is passed in (refresher can't import app -- app imports it) so
    # the sweep's re-generations contend for the same cap as the request paths.
    background_tasks.add_task(refresher.refresh_due_sites, make_client, _generation_slots)
    return {"status": "triggered"}


@app.post("/check-changes")
async def check_changes(request: GenerateRequest) -> dict:
    base_url, scope_prefix = scope_of(request.url)
    try:
        prior = await run_in_threadpool(db.load_generation, base_url)
    except Exception:
        prior = None

    # Re-generate with the SAME settings the file was created with, so the hash
    # comparison is apples-to-apples (different discovery => false "changed").
    if prior:
        # Rows written before the honor_robots column existed replay as honoring it.
        settings = {k: prior[k] for k in ("crawl", "max_pages", "enhance", "bypass")}
        settings["honor_robots"] = prior.get("honor_robots", True)
    else:
        settings = {
            "crawl": True, "max_pages": 50, "enhance": False, "bypass": False,
            "honor_robots": True,
        }

    # Under the same cap: this replays the prior settings, bypass=True included, so
    # an uncapped /check-changes would be a paid-session fan-out hole. Queues when full.
    async with _generation_slots:
        async with make_client() as client:
            result = await generate(
                request.url, client, settings["max_pages"], settings["crawl"],
                settings["enhance"], settings["bypass"],
                honor_robots=settings["honor_robots"],
            )

    # A zero-page result here is a transient failure, not a real change. Uploading
    # now would clobber the good S3 object with a near-empty one, so leave the file
    # and DB row untouched and report the check as inconclusive.
    if not result["pages"]:
        return {
            "changed": False,
            "regenerated_llms_txt": None,
            "public_url": prior["public_url"] if prior else None,
            "details": "change check skipped: no pages fetched (transient failure?)",
        }

    new_hash = db.site_hash(result["pages"])
    changed = prior is None or prior["content_hash"] != new_hash

    object_key, public_url = await run_in_threadpool(
        _store_file, base_url, result["llms_txt"], result["warnings"]
    )
    await run_in_threadpool(
        _persist, base_url, scope_prefix, result, settings, object_key, public_url
    )

    if prior is None:
        details = "no prior generation"
    elif changed:
        details = "changed"
    else:
        details = "no changes"
    return {
        "changed": changed,
        "regenerated_llms_txt": result["llms_txt"] if changed else None,
        "public_url": public_url,
        "details": details,
    }

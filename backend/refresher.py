"""Nightly auto-update sweep -- the cron counterpart of /check-changes.

A scheduler hits /internal/cron/refresh and every due site is re-checked: a cheap
sitemap-lastmod probe first, a full re-generation (with the site's stored settings)
only when the sitemap says something changed."""

import asyncio
from contextlib import nullcontext
import logging
from datetime import datetime, timezone

import httpx
from starlette.concurrency import run_in_threadpool

import db
import storage
from discoverer import RobotsRules, discover, read_robots
from fetcher import fetch
from generator import generate
from urls import origin_of, scope_of


# Named so a demo/operator can `aws logs tail /ecs/llms-backend | grep refresh`.
# Child of uvicorn's logger, so it inherits uvicorn's handler + INFO level.
_log = logging.getLogger("uvicorn.refresh")


def _parse_lastmod(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    # Date-only lastmods parse naive; pin them to UTC so every comparison is
    # between aware datetimes (stripping tzinfo instead can misorder values
    # from sites in different offsets).
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class _ProbeMemoClient:
    """httpx.AsyncClient wrapper that memoizes GET responses.

    The freshness probe and the full generation both fetch robots.txt and the
    sitemap; caching the probe's responses hits the site once, not twice.
    Recording stops after the probe so page fetches aren't held. HEAD/POST pass
    through untouched.
    """

    def __init__(self, inner: httpx.AsyncClient):
        self._inner = inner
        self._cache: dict[str, httpx.Response] = {}
        # Concurrent fetches can miss the cache for the same URL simultaneously;
        # a per-key lock makes the first caller fetch and the rest wait for it.
        self._locks: dict[str, asyncio.Lock] = {}
        self.recording = True

    async def get(self, url, **kwargs) -> httpx.Response:
        key = str(url)
        if key in self._cache:
            return self._cache[key]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._cache:
                return self._cache[key]
            response = await self._inner.get(url, **kwargs)
            if self.recording:
                self._cache[key] = response
            return response

    async def head(self, url, **kwargs) -> httpx.Response:
        return await self._inner.head(url, **kwargs)

    async def post(self, url, **kwargs) -> httpx.Response:
        # generate() hands this client to curate(), which POSTs to OpenRouter;
        # without this passthrough an enhance=True refresh would break.
        return await self._inner.post(url, **kwargs)


async def _newest_sitemap_lastmod(site_url: str, client, settings: dict) -> datetime | None:
    """Cheap freshness probe: robots.txt + sitemap XML only, no page fetches.

    Reuses the real discovery path (crawl=False fetches no pages) so robots
    handling and scope filtering match what a full generation would consider.
    Returns None when the site has no usable sitemap or no lastmod values --
    the gate is then inconclusive.
    """
    base_url, scope_prefix = scope_of(site_url)

    async def fetch_fn(target):
        return await fetch(target, client)

    rules = await read_robots(origin_of(base_url), fetch_fn)
    if not settings["honor_robots"]:
        # Same policy seam as generate(): keep Sitemap hints, drop restrictions.
        rules = RobotsRules(sitemaps=rules.sitemaps)
    candidates = await discover(base_url, fetch_fn, settings["max_pages"], False,
                                scope_prefix, robots=rules)
    lastmods = [parsed for page in candidates
                if (parsed := _parse_lastmod(page.lastmod)) is not None]
    return max(lastmods, default=None)


async def refresh_site(site_url: str, client_factory, slots=None) -> str:
    """Re-check one site, re-generating only when the sitemap says it changed.

    Returns "changed", "unchanged", or "skipped" (no prior row, or a transient
    empty crawl -- same no-clobber stance as /check-changes).

    `slots` is app.py's generation semaphore, passed in (refresher can't import
    app) so the sweep's crawls share the same cap as the request paths -- a refresh
    can replay bypass=True and open a PAID session. The cheap probe stays outside
    the cap: no page fetches, no browser.
    """
    prior = await run_in_threadpool(db.load_generation, site_url)
    if prior is None:
        return "skipped"

    # Replay the exact settings the file was generated with, so the hash
    # comparison is apples-to-apples (different discovery => false "changed").
    settings = {k: prior[k] for k in ("crawl", "max_pages", "enhance", "bypass")}
    settings["honor_robots"] = prior.get("honor_robots", True)

    async with client_factory() as raw_client:
        client = _ProbeMemoClient(raw_client)
        # Freshness gate: skip the crawl if the sitemap's newest <lastmod> hasn't
        # advanced past the last baseline. An inconclusive probe falls through to
        # the full pipeline -- the gate only ever skips work, never invents a change.
        observed = await _newest_sitemap_lastmod(site_url, client, settings)
        prior_lastmod = prior.get("sitemap_newest_lastmod")
        if observed is not None and prior_lastmod is not None and observed <= prior_lastmod:
            return "unchanged"

        client.recording = False
        async with (slots or nullcontext()):
            result = await generate(
                site_url, client, settings["max_pages"], settings["crawl"],
                settings["enhance"], settings["bypass"],
                honor_robots=settings["honor_robots"],
            )

    # Zero pages is a transient crawl failure, not a site-emptied-out change:
    # leave the durable S3 object and the last-good DB row untouched (and don't
    # record the observed lastmod, or the gate would skip the retry next tick).
    if not result["pages"]:
        return "skipped"

    new_hash = db.site_hash(result["pages"])
    changed = prior["content_hash"] != new_hash

    if changed:
        base_url, scope_prefix = scope_of(site_url)
        object_key = storage.object_key_for(base_url)
        try:
            public_url = await run_in_threadpool(
                storage.upload_llms_txt, result["llms_txt"], object_key
            )
        except Exception:  # noqa: BLE001 - keep the last-good pointer, not None
            object_key, public_url = prior.get("object_key"), prior.get("public_url")

        await run_in_threadpool(db.save_generation, {
            "site_url": base_url,
            "scope_prefix": scope_prefix,
            "content_hash": new_hash,
            "page_count": len(result["pages"]),
            "warnings": result["warnings"],
            "object_key": object_key,
            "public_url": public_url,
            **settings,
        })

    # Record the baseline after every full run (changed or not), so a lastmod bump
    # with identical content doesn't force a full crawl every tick.
    if observed is not None:
        try:
            await run_in_threadpool(db.record_sitemap_lastmod, site_url, observed)
        except Exception:  # noqa: BLE001 - the gate is an optimization only
            pass

    return "changed" if changed else "unchanged"


async def refresh_due_sites(client_factory=None, slots=None) -> dict:
    factory = client_factory or (lambda: httpx.AsyncClient())
    counts = {"due": 0, "changed": 0, "unchanged": 0, "skipped": 0}
    try:
        due = await run_in_threadpool(db.load_due_sites)
    except Exception:  # noqa: BLE001 - DB down: nothing to refresh this tick
        return counts
    counts["due"] = len(due)
    _log.info("refresh sweep: %d site(s) due", len(due))

    for site_url in due:
        try:
            outcome = await refresh_site(site_url, factory, slots)
        except Exception:  # noqa: BLE001 - one bad site must not stop the sweep
            _log.exception("refresh sweep: %s errored", site_url)
            outcome = "skipped"
        counts[outcome] += 1
        _log.info("refresh sweep: %s -> %s", site_url, outcome)
        try:
            await run_in_threadpool(db.schedule_next_check, site_url)
        except Exception:  # noqa: BLE001
            pass

    _log.info("refresh sweep: done -- %d due, %d changed, %d unchanged, %d skipped",
              counts["due"], counts["changed"], counts["unchanged"], counts["skipped"])
    return counts
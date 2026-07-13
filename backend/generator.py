import asyncio
from contextlib import nullcontext
from dataclasses import asdict

import httpx

from browser import BROWSER_CONCURRENCY, browser_fetch, browser_session
from curator import curate
from discoverer import RobotsRules, discover, read_robots
from extractor import extract
from fetcher import FETCH_CONCURRENCY, FetchResult, fetch, head, is_blocked, is_challenge
from formatter import format_llms_txt
from models import PageInfo
from progress import Event, emit as _emit, noop
from ranker import rank
from urls import (is_disallowed, is_metadata_file, md_twin_of, normalize,
                  origin_of, scope_of)
from validator import validate


class _Stage:
    """The phase label the fetch wrapper stamps on every frame.

    Phases in generate() run sequentially -- concurrency exists only *within* a
    phase -- so one mutable holder labels every frame correctly, and no
    signature anywhere else has to carry a stage through.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


async def _extract_pages(discovered, fetch_fn, warnings: list[str], delay: float = 0.0,
                         limit: int | None = None, on_event=noop):
    # `discovered` holds surplus candidates (see discoverer.CANDIDATE_SURPLUS);
    # the limit counts successes, so fetch failures backfill from the surplus.
    #
    # Results are collected by candidate index, never by completion order:
    # ranker.rank sorts stably on a key that ties for nearly every page, so this
    # list's order decides the section and link order of the final document.
    pages: list[PageInfo] = []

    # The counter counts pages ADMITTED, not fetches completed. BFS candidates
    # arrive already extracted (they have a title) and are never re-fetched, so a
    # fetch-completion counter would read 0/N for an entire crawl on any site
    # without a sitemap.
    total = len(discovered) if limit is None else min(limit, len(discovered))

    def admit(page: PageInfo) -> None:
        pages.append(page)
        _emit(on_event, Event(stage="page", phase="done", url=page.url,
                              done=len(pages), total=total))

    async def fetch_one(candidate) -> PageInfo | None:
        response = await fetch_fn(candidate.url)
        if not response.ok:
            warnings.append(f"skipped {candidate.url} (status {response.status})")
            return None
        page = extract(response.text, response.url)
        page.lastmod = candidate.lastmod
        page.priority = candidate.priority
        return page

    start = 0
    while start < len(discovered) and (limit is None or len(pages) < limit):
        # Already-extracted BFS candidates cost no fetch, so they must not
        # consume a wave slot. Walk forward until the wave holds
        # FETCH_CONCURRENCY *fetch-needing* candidates.
        end = start
        needed = 0
        # Clamp width to >= 1: a width of 0 would make no progress and spin this
        # loop with no await, freezing the event loop.
        wave_width = max(1, FETCH_CONCURRENCY)
        while end < len(discovered) and needed < wave_width:
            if not discovered[end].title:
                needed += 1
            end += 1
        window = discovered[start:end]
        pending = [candidate for candidate in window if not candidate.title]

        if delay > 0:
            # robots.txt Crawl-delay means "wait N seconds BETWEEN requests" --
            # concurrency would violate it. Fall back to strictly sequential.
            fetched = []
            for candidate in pending:
                fetched.append(await fetch_one(candidate))
                await asyncio.sleep(delay)
        else:
            outcomes = await asyncio.gather(
                *(fetch_one(c) for c in pending), return_exceptions=True
            )
            # Collect exceptions rather than let gather propagate the first raise
            # and orphan its siblings: a bad page fails only its own slot, which the
            # surplus backfills, leaving order untouched.
            fetched = []
            for candidate, outcome in zip(pending, outcomes):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                if isinstance(outcome, BaseException):
                    warnings.append(f"skipped {candidate.url} ({type(outcome).__name__})")
                    outcome = None
                fetched.append(outcome)

        # Reassemble in candidate order -- never completion order.
        results = iter(fetched)
        for candidate in window:
            if limit is not None and len(pages) >= limit:
                break
            if candidate.title:
                admit(candidate)
                continue
            page = next(results, None)
            if page is not None:
                admit(page)

        start = end

    return pages


# A 200 alone can't be trusted for a twin: SPAs answer every path with their
# HTML shell, so the response must also declare a markdown/plain content type.
_MD_TWIN_CONTENT_TYPES = ("text/markdown", "text/x-markdown", "text/plain")


async def _probe_md_twins(pages, head_fn, disallow: list[str], allow=(),
                          delay: float = 0.0):
    # Docs hosts mirror each page as raw markdown -- the friendlier link target for
    # an LLM. Each coroutine mutates a distinct PageInfo so there is no race; the
    # semaphore just bounds concurrent HEADs (max_pages=None makes `pages`
    # unbounded). Clamp to >= 1 -- Semaphore(0) would deadlock.
    semaphore = asyncio.Semaphore(1 if delay > 0 else max(1, FETCH_CONCURRENCY))

    async def probe(page) -> None:
        twin = md_twin_of(page.url)
        # No twin to probe: the page is already markdown. formatter falls back to
        # `page.url`, which is the .md URL anyway.
        if twin is None or is_disallowed(twin, disallow, allow):
            return
        async with semaphore:
            status, content_type = await head_fn(twin)
            if delay > 0:
                await asyncio.sleep(delay)
        if status == 200 and content_type.startswith(_MD_TWIN_CONTENT_TYPES):
            page.md_url = twin

    # Best-effort: a failed probe just means "no twin", so swallow exceptions.
    await asyncio.gather(*(probe(page) for page in pages), return_exceptions=True)


def _drop_site_level_descriptions(pages: list[PageInfo], home_url: str,
                                  site_summary: str | None) -> None:
    """Blank a description that is really the SITE's, not the page's.

    A page with no <meta name="description"> falls back to og:description, which
    sites set once site-wide -- so e.g. a Privacy page ends up described by the
    site blurb. A wrong description is worse than none, so blank it. The homepage
    keeps it, where it is true.
    """
    if not site_summary:
        return
    home_key = normalize(home_url)
    for page in pages:
        if normalize(page.url) != home_key and page.description == site_summary:
            page.description = None


def _dedupe_identical_entries(pages: list[PageInfo]) -> list[PageInfo]:
    """Drop rows an LLM has no way to tell apart.

    Sites often link the same page many times with only a query param differing --
    same title, same description, several identical rows. Only pages that HAVE a
    description are deduped: two descriptionless pages sharing a title may still be
    different pages, and collapsing them would lose one.
    """
    seen: set[tuple[str, str]] = set()
    result: list[PageInfo] = []
    for page in pages:
        if page.description:
            key = (page.title, page.description)
            if key in seen:
                continue
            seen.add(key)
        result.append(page)
    return result


def _dedupe_by_link_target(pages: list[PageInfo]) -> list[PageInfo]:
    """Drop pages that would emit a link a URL another page already links to.

    When a sitemap lists both an HTML page and its .md twin, _probe_md_twins makes
    the HTML page's link target the .md URL -- the same URL the standalone .md page
    links to -- so the target appears twice. discover()'s dedupe on page.url can't
    see this; the collision only exists once md_url is known. First-wins keeps the
    HTML page, which carries the richer og:title/meta while still linking the .md.
    Filters in place: this list's order decides the document order (rank sorts
    stably), so reordering here would rewrite the document.
    """
    seen: set[str] = set()
    kept: list[PageInfo] = []
    for page in pages:
        key = normalize(page.md_url or page.url)
        if key in seen:
            continue
        seen.add(key)
        kept.append(page)
    return kept


async def generate(
    url: str,
    client: httpx.AsyncClient,
    max_pages: int | None = 50,
    crawl: bool = True,
    enhance: bool = False,
    bypass: bool = False,
    honor_robots: bool = True,
    on_event=noop,
) -> dict:
    # A bare domain generates a whole-site llms.txt; a deeper URL scopes to that
    # subpath (the section it belongs to), per the spec's optional-subpath rule.
    base_url, scope_prefix = scope_of(url)
    warnings: list[str] = []
    stage = _Stage("robots")

    session = browser_session() if bypass else nullcontext(None)
    async with session as browser:
        if bypass:
            # Per generation: the gate only protects this browser handle's lifetime.
            browser_gate = asyncio.Semaphore(BROWSER_CONCURRENCY)
            if browser is None:
                warnings.append(
                    "unblock enabled but browser unavailable "
                    "(check BRIGHT_DATA_CDP_URL) — using direct fetch"
                )

        # The homepage is fetched here (for title/summary) and again as the BFS
        # seed. Memoize it so the crawl reuses this response instead of a second
        # fetch -- a wasted round trip, or a wasted PAID render under bypass.
        home_target = base_url + "/" if scope_prefix is None else base_url
        homepage: FetchResult | None = None
        # URLs served a bot wall we couldn't get past. A wall answers 200 with a
        # complete page, so the crawl "succeeds" -- track them to warn the user.
        walled: list[str] = []

        async def fetch_fn(target: str) -> FetchResult:
            # Match the RAW url (rstrip only the trailing slash): normalize() also
            # folds /index.html into the root, and serving that from the homepage's
            # response would rewrite the page's url and change the document.
            if homepage is not None and target.rstrip("/") == home_target.rstrip("/"):
                return homepage
            _emit(on_event, Event(stage=stage.name, phase="start", url=target))
            result = await fetch(target, client)
            used_browser = False
            # robots.txt and sitemaps are not pages -- a browser can't read them and
            # a rendered sitemap stops being valid XML. Never escalate them.
            if is_blocked(result) and not is_metadata_file(target):
                # is_blocked also fires on the shell heuristic (a guess); only a
                # challenge is certain enough to report as a wall.
                wall = is_challenge(result)
                if browser is not None:
                    async with browser_gate:
                        rendered = await browser_fetch(browser, target, warnings)
                    if rendered.ok:
                        result = rendered
                        used_browser = True
                    elif wall:
                        walled.append(target)
                elif wall:
                    walled.append(target)
            _emit(on_event, Event(stage=stage.name, phase="done", url=target,
                                  status=result.status, ok=result.ok,
                                  browser=used_browser))
            return result

        async def head_fn(target: str) -> tuple[int, str]:
            # For a twin probe, `ok` must mean "a twin was found", not "the server
            # answered": an SPA answers every path with its HTML shell (200), so
            # require a markdown content-type, not just a 200.
            _emit(on_event, Event(stage=stage.name, phase="start", url=target))
            status, content_type = await head(target, client)
            found = status == 200 and content_type.startswith(_MD_TWIN_CONTENT_TYPES)
            _emit(on_event, Event(stage=stage.name, phase="done", url=target,
                                  status=status, ok=found))
            return status, content_type

        # Robots rules must be known before anything else is fetched — the
        # homepage itself may be disallowed (e.g. `Disallow: /`).
        rules = await read_robots(origin_of(base_url), fetch_fn,
                                  warnings if honor_robots else None)
        if not honor_robots:
            # The single policy seam: keep the useful part (Sitemap hints),
            # drop every restriction. Downstream consumers of `rules` stay
            # policy-free and need no knowledge of the toggle.
            rules = RobotsRules(sitemaps=rules.sitemaps)
            warnings.append("robots.txt restrictions ignored (honor robots.txt is off)")

        stage.name = "home"
        # Whole-site: the origin root. Subpath: the section landing. (Computed
        # above, so fetch_fn can memoize it.)
        if is_disallowed(home_target, rules.disallow, rules.allow):
            warnings.append("homepage disallowed by robots.txt — not fetched")
            homepage = FetchResult(url=home_target, status=0, text="", ok=False)
        else:
            homepage = await fetch_fn(home_target)
            if not homepage.ok:
                warnings.append(f"homepage returned status {homepage.status}")
        home_page = extract(homepage.text if homepage.ok else "", home_target)

        if max_pages is None:
            warnings.append("no page limit set — fetched all discovered pages")

        stage.name = "discover"
        discovered = await discover(base_url, fetch_fn, max_pages, crawl, scope_prefix,
                                    robots=rules, warnings=warnings)
        _emit(on_event, Event(stage="discover", message=f"{len(discovered)} candidates"))
        if not discovered:
            warnings.append("no pages discovered")

        stage.name = "extract"
        # No pause between fetches unless robots.txt asks for one via Crawl-delay.
        pages = await _extract_pages(discovered, fetch_fn, warnings, rules.crawl_delay or 0.0,
                                     limit=max_pages, on_event=on_event)

        # Design §7: the homepage must always be represented in `pages`.
        if homepage.ok:
            home_page.url = homepage.url
            # normalize(), not a hand-rolled rstrip: the page list is deduped by
            # normalize(), so anything weaker disagrees with it and re-inserts a
            # homepage that is already there under another spelling (/index.html).
            home_key = normalize(home_page.url)
            if not any(normalize(p.url) == home_key for p in pages):
                pages.insert(0, home_page)

        stage.name = "twins"
        # Probes bypass the browser-escalation path on purpose: a browser can't
        # HEAD, and a blocked probe just means "no twin" -- the safe default.
        await _probe_md_twins(pages, head_fn,
                              rules.disallow, rules.allow, rules.crawl_delay or 0.0)
        # Only here is the effective link target (md_url or url) known, so only
        # here can two pages be seen to point at the same URL. Dedupe the real
        # list so the document, the API response and db.site_hash all agree.
        pages = _dedupe_by_link_target(pages)

        # Blank site-level descriptions FIRST, so pages that merely share the site
        # blurb aren't then collapsed into one by the identical-entry dedupe.
        _drop_site_level_descriptions(pages, home_page.url, home_page.description)
        pages = _dedupe_identical_entries(pages)

        # A bot wall answers 200 with a complete page, so a fully-walled crawl
        # "succeeds" and produces a spec-compliant document made entirely of
        # interstitials. Say so plainly, and name the lever that fixes it -- a
        # silent garbage file that gets uploaded and served to LLMs is the worst
        # outcome here, worse than an error.
        if walled:
            # Distinct URLs, no denominator: `walled` counts every walled request
            # (discovery, BFS, surplus), not just pages, so "N of len(pages)" lies.
            count = len(set(walled))
            noun = "page" if count == 1 else "pages"
            if browser is None:
                warnings.append(
                    f"{count} {noun} were served a bot-protection wall, not real content — "
                    "enable “Unblock protected sites” to render them through a real browser"
                )
            else:
                warnings.append(
                    f"{count} {noun} were served a bot-protection wall that the browser "
                    "could not get past either"
                )

        site = rank(pages, title=home_page.title, summary=home_page.description)
        _emit(on_event, Event(stage="rank",
                              message=f"{len(pages)} pages → {len(site.sections)} sections"))

        if enhance:
            # ONE LLM call with a 30s timeout -- emit BEFORE it, or the log freezes.
            stage.name = "curate"
            _emit(on_event, Event(stage="curate", phase="start",
                                  message=f"asking the model to curate {len(pages)} pages"))
            site = await curate(site, client, warnings)
            _emit(on_event, Event(stage="curate", phase="done"))

        document = format_llms_txt(site)
        _emit(on_event, Event(stage="format", message=f"{len(document)} bytes"))

        result = validate(document)
        warnings.extend(f"validation: {error}" for error in result.errors)
        _emit(on_event, Event(stage="validate",
                              message="spec-compliant" if not result.errors
                                      else f"{len(result.errors)} validation issues"))

        return {
            "llms_txt": document,
            "pages": [asdict(page) for page in pages],
            "warnings": warnings,
        }

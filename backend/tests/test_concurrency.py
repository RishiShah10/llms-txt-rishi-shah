import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

import generator as generator_mod
from browser import BROWSER_CONCURRENCY
from crawler import crawl
from discoverer import discover, RobotsRules
from fetcher import FETCH_CONCURRENCY, FetchResult
from generator import _extract_pages, _probe_md_twins
from models import PageInfo

pytestmark = pytest.mark.anyio


class _Response:
    def __init__(self, url, text, ok=True, status=200):
        self.url, self.text, self.ok, self.status = url, text, ok, status


def _candidates(n):
    return [PageInfo(url=f"https://a.com/p{i}", title="") for i in range(n)]


async def test_results_keep_candidate_order_not_completion_order():
    # ranker.py sorts stably on a key that is 0 for nearly every page, so the
    # page list order passes straight through into section order and link order
    # in the final llms.txt. A gather that appends on completion silently
    # produces a different document -- and db.site_hash sorts before hashing, so
    # change-detection would NOT catch it.
    async def fetch_fn(url):
        index = int(url.rsplit("p", 1)[1])
        # Invert the delay so later pages finish FIRST.
        await asyncio.sleep((10 - index) * 0.01)
        return _Response(url, f"<html><title>P{index}</title></html>")

    pages = await _extract_pages(_candidates(10), fetch_fn, [], limit=10)

    assert [page.url for page in pages] == [f"https://a.com/p{i}" for i in range(10)]


async def test_concurrency_is_bounded():
    in_flight = 0
    peak = 0

    async def fetch_fn(url):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return _Response(url, "<html><title>x</title></html>")

    await _extract_pages(_candidates(50), fetch_fn, [], limit=50)

    from fetcher import FETCH_CONCURRENCY
    assert peak <= FETCH_CONCURRENCY, f"peak in-flight was {peak}"
    assert peak > 1, "fetches did not run concurrently at all"


async def test_wave_loop_stops_at_limit_successes():
    # discover() hands over a ~2x surplus (CANDIDATE_SURPLUS). The sequential
    # code stopped at `limit` successes for free; the wave loop must too, or we
    # fetch twice the pages we use.
    calls = []

    async def fetch_fn(url):
        calls.append(url)
        return _Response(url, "<html><title>x</title></html>")

    pages = await _extract_pages(_candidates(100), fetch_fn, [], limit=50)

    assert len(pages) == 50
    from fetcher import FETCH_CONCURRENCY
    assert len(calls) < 50 + FETCH_CONCURRENCY, f"overfetched: {len(calls)} calls for 50 pages"


async def test_already_extracted_pages_are_not_refetched():
    # BFS-crawled candidates arrive with a title already set. They must pass
    # through free and must not consume a wave slot.
    calls = []

    async def fetch_fn(url):
        calls.append(url)
        return _Response(url, "<html><title>x</title></html>")

    discovered = [
        PageInfo(url="https://a.com/bfs1", title="Already Done"),
        PageInfo(url="https://a.com/sm1", title=""),
        PageInfo(url="https://a.com/bfs2", title="Also Done"),
    ]
    pages = await _extract_pages(discovered, fetch_fn, [], limit=3)

    assert calls == ["https://a.com/sm1"]
    assert [page.url for page in pages] == [
        "https://a.com/bfs1", "https://a.com/sm1", "https://a.com/bfs2",
    ]


async def test_crawl_delay_forces_sequential():
    # robots.txt Crawl-delay means "wait N seconds BETWEEN requests". Firing ten
    # at once violates it outright.
    in_flight = 0
    peak = 0

    async def fetch_fn(url):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return _Response(url, "<html><title>x</title></html>")

    await _extract_pages(_candidates(10), fetch_fn, [], delay=0.001, limit=10)

    assert peak == 1, f"Crawl-delay was set but {peak} requests overlapped"


async def test_zero_fetch_concurrency_does_not_hang():
    # If FETCH_CONCURRENCY is ever set to 0 (or negative), the wave-width loop
    # must not spin forever with no await. The loop would never set needed >= 0,
    # end stays at start, and start = end makes no progress. Clamping the wave
    # width to at least 1 prevents this.
    import generator

    original = generator.FETCH_CONCURRENCY
    try:
        generator.FETCH_CONCURRENCY = 0

        async def fetch_fn(url):
            return _Response(url, "<html><title>x</title></html>")

        # Must complete within 5 seconds; without the clamp, it would hang forever.
        pages = await asyncio.wait_for(
            _extract_pages(_candidates(5), fetch_fn, [], limit=5),
            timeout=5
        )

        assert len(pages) == 5, f"Expected 5 pages, got {len(pages)}"
    finally:
        generator.FETCH_CONCURRENCY = original


async def test_twin_probes_are_concurrent_and_bounded():
    in_flight = 0
    peak = 0

    async def head_fn(url):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return 200, "text/markdown"

    pages = [PageInfo(url=f"https://a.com/p{i}.html", title=f"P{i}") for i in range(30)]
    await _probe_md_twins(pages, head_fn, [], ())

    from fetcher import FETCH_CONCURRENCY
    assert peak > 1, "twin probes did not run concurrently"
    assert peak <= FETCH_CONCURRENCY, f"unbounded: peak in-flight was {peak}"
    assert all(page.md_url is not None for page in pages)


async def test_twin_probes_serialize_under_crawl_delay():
    in_flight = 0
    peak = 0

    async def head_fn(url):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return 200, "text/markdown"

    pages = [PageInfo(url=f"https://a.com/p{i}.html", title=f"P{i}") for i in range(5)]
    await _probe_md_twins(pages, head_fn, [], (), delay=0.001)

    assert peak == 1


async def test_bfs_fetches_a_level_concurrently():
    # This is the bug the first draft of the spec would have shipped: BFS was
    # handed delay=max(CRAWL_DELAY_SECONDS, delay), which is never zero, so a
    # gate keyed on `delay` would have serialized BFS 100% of the time.
    in_flight = 0
    peak = 0
    home = "".join(f'<a href="https://a.com/p{i}">p{i}</a>' for i in range(20))

    async def fetch_fn(url):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        body = home if url.rstrip("/") == "https://a.com" else "<html><title>x</title></html>"
        return _Response(url, f"<html><body>{body}</body></html>")

    await crawl("https://a.com", fetch_fn, set(), [], limit=20, delay=0)

    assert peak > 1, "BFS did not fetch a level concurrently"


_INDEX = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
%s
</sitemapindex>""" % "\n".join(
    f"<sitemap><loc>https://a.com/sm{i}.xml</loc></sitemap>" for i in range(15)
)

_CHILD = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://a.com/page-%s</loc></url>
</urlset>"""


async def test_nested_sitemaps_are_fetched_concurrently():
    in_flight = 0
    peak = 0

    async def fetch_fn(url):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        if url.endswith("/sitemap.xml"):
            return _Response(url, _INDEX)
        if "/sm" in url:
            return _Response(url, _CHILD % url.rsplit("sm", 1)[1].split(".")[0])
        return _Response(url, "", ok=False, status=404)

    rules = RobotsRules(sitemaps=["https://a.com/sitemap.xml"])
    pages = await discover("https://a.com", fetch_fn, max_pages=None, crawl=False,
                           robots=rules)

    # Siblings go out together: the index has 15 children, so the first batch
    # should saturate FETCH_CONCURRENCY.
    assert peak == FETCH_CONCURRENCY, (
        f"nested sitemaps did not saturate the batch width (peak={peak})"
    )

    # Concurrency must buy speed and nothing else: the pages must come back in
    # sitemap-index document order, exactly as a sequential walk produced them.
    # `peak > 1` alone says nothing about the order the responses are consumed in.
    assert [page.url for page in pages] == [
        f"https://a.com/page-{i}" for i in range(15)
    ]


_BLOCKED_SITEMAP = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
%s
</urlset>""" % "\n".join(
    f"<url><loc>https://blocked.com/p{i}</loc></url>" for i in range(20)
)


async def test_browser_escalation_is_bounded(monkeypatch):
    # Every page 403s, so is_blocked() fires and each fetch escalates to the
    # PAID browser. FETCH_CONCURRENCY is 10, but the browser gate must hold the
    # line at BROWSER_CONCURRENCY (2) -- ten concurrent Chromium pages on a
    # metered Bright Data session is both slow and expensive.
    in_flight = 0
    peak = 0

    async def fake_browser_fetch(browser, url, warnings):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return FetchResult(url=url, status=200, text="<html><title>R</title></html>", ok=True)

    @asynccontextmanager
    async def fake_session():
        yield object()  # a non-None sentinel: "a browser is available"

    monkeypatch.setattr(generator_mod, "browser_fetch", fake_browser_fetch)
    monkeypatch.setattr(generator_mod, "browser_session", fake_session)

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=_BLOCKED_SITEMAP)
        return httpx.Response(403, text="Forbidden")  # -> is_blocked() -> escalate

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await generator_mod.generate(
            "https://blocked.com", client, max_pages=20, crawl=False, bypass=True
        )

    assert peak > 1, "browser escalations did not overlap at all"
    assert peak <= BROWSER_CONCURRENCY, (
        f"{peak} concurrent PAID browser sessions, cap is {BROWSER_CONCURRENCY}"
    )


# --------------------------------------------------------------------------
# A raising sibling must fail only its own slot.
#
# Plain asyncio.gather propagates the first exception immediately WITHOUT
# cancelling its siblings; those orphans keep running against an AsyncClient that
# app.py's `async with` then closes ("client has been closed" / "Task exception
# was never retrieved"). The batch collects exceptions instead, and a raising
# fetch is coerced to a failed FetchResult occupying the slot its response would
# have -- so ordering and the page set stay exactly as they were.
# --------------------------------------------------------------------------

_SIBLING_INDEX = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
%s
</sitemapindex>""" % "\n".join(
    f"<sitemap><loc>https://a.com/sm{i}.xml</loc></sitemap>" for i in range(4)
)


async def test_a_raising_sitemap_child_does_not_fail_its_batch():
    async def fetch_fn(url):
        if url.endswith("/sitemap.xml"):
            return _Response(url, _SIBLING_INDEX)
        if url.endswith("/sm2.xml"):
            raise httpx.ConnectError("boom")  # one bad sibling
        if "/sm" in url:
            return _Response(url, _CHILD % url.rsplit("sm", 1)[1].split(".")[0])
        return _Response(url, "", ok=False, status=404)

    rules = RobotsRules(sitemaps=["https://a.com/sitemap.xml"])
    pages = await discover("https://a.com", fetch_fn, max_pages=None, crawl=False,
                           robots=rules)

    # sm2 fails its own slot; every other sibling survives, in document order.
    assert [page.url for page in pages] == [
        "https://a.com/page-0", "https://a.com/page-1", "https://a.com/page-3",
    ]


async def test_a_raising_page_does_not_fail_its_extract_wave():
    discovered = [PageInfo(url=f"https://a.com/p{i}", title="") for i in range(4)]

    async def fetch_fn(url):
        if url.endswith("/p1"):
            raise httpx.ConnectError("boom")
        return _Response(url, f"<html><title>T{url[-1]}</title><body>body</body></html>")

    warnings: list[str] = []
    pages = await generator_mod._extract_pages(discovered, fetch_fn, warnings)

    # The raiser is skipped; the rest keep their candidate order.
    assert [page.url for page in pages] == [
        "https://a.com/p0", "https://a.com/p2", "https://a.com/p3",
    ]
    assert any("https://a.com/p1" in w for w in warnings)

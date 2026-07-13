import os
from contextlib import asynccontextmanager

import httpx
import pytest

import generator as generator_mod
from generator import generate, _extract_pages
from models import PageInfo
from fetcher import FetchResult

pytestmark = pytest.mark.anyio

HOME = "<html><head><title>Acme Inc</title><meta name='description' content='We do things'></head></html>"
SITEMAP = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://a.com/docs/start</loc></url>
</urlset>"""
DOC = "<html><head><title>Start</title><meta name='description' content='begin here'></head></html>"

PAGES = {
    "https://a.com/robots.txt": "Sitemap: https://a.com/sitemap.xml",
    "https://a.com/sitemap.xml": SITEMAP,
    "https://a.com/": HOME,
    "https://a.com/docs/start": DOC,
}


def handler(request):
    url = str(request.url)
    return httpx.Response(200, text=PAGES[url]) if url in PAGES else httpx.Response(404, text="")


async def test_generate_end_to_end():
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await generate("https://a.com", client)
    assert result["llms_txt"].startswith("# Acme Inc")
    assert "## Docs" in result["llms_txt"]
    assert "[Start](https://a.com/docs/start): begin here" in result["llms_txt"]
    assert any(p["url"] == "https://a.com/docs/start" for p in result["pages"])


async def test_homepage_included_even_when_absent_from_sitemap():
    # Design §7: the homepage must always be represented in `pages`, even if
    # the sitemap only lists other URLs.
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await generate("https://a.com", client)
    urls = [p["url"] for p in result["pages"]]
    assert "https://a.com/" in urls


SCOPED_SITEMAP = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://a.com/docs/intro</loc></url>
  <url><loc>https://a.com/docs/api</loc></url>
  <url><loc>https://a.com/blog/post</loc></url>
</urlset>"""

SCOPED_PAGES = {
    "https://a.com/robots.txt": "",
    "https://a.com/sitemap.xml": SCOPED_SITEMAP,
    "https://a.com/docs/": "<html><head><title>Docs Home</title></head></html>",
    "https://a.com/docs/intro": "<html><head><title>Intro</title></head></html>",
    "https://a.com/docs/api": "<html><head><title>API</title></head></html>",
    "https://a.com/blog/post": "<html><head><title>Blog</title></head></html>",
}


def scoped_handler(request):
    url = str(request.url)
    return httpx.Response(200, text=SCOPED_PAGES[url]) if url in SCOPED_PAGES else httpx.Response(404, text="")


async def test_subpath_url_scopes_to_that_section():
    # A deep URL generates an llms.txt for that subpath and everything beneath
    # it -- so /docs pages are included and the out-of-scope /blog page is not.
    async with httpx.AsyncClient(transport=httpx.MockTransport(scoped_handler)) as client:
        result = await generate("https://a.com/docs/", client)
    urls = [p["url"] for p in result["pages"]]
    assert "https://a.com/docs/intro" in urls
    assert "https://a.com/docs/api" in urls
    assert "https://a.com/blog/post" not in urls


async def test_extract_pages_reuses_already_extracted():
    calls = []

    async def fetch_fn(url):
        calls.append(url)
        return FetchResult(url=url, status=200, text="<title>Fetched</title>", ok=True)

    discovered = [
        PageInfo(url="https://a.com/bfs", title="Crawled", description="d"),
        PageInfo(url="https://a.com/sm", title=""),
    ]
    pages = await _extract_pages(discovered, fetch_fn, [])

    assert calls == ["https://a.com/sm"]           # only the sitemap page was fetched
    titles = {p.title for p in pages}
    assert "Crawled" in titles                     # BFS page reused, not re-fetched
    assert "Fetched" in titles                     # sitemap page fetched + extracted


async def test_backfills_failed_pages_from_surplus_candidates():
    # A stale sitemap 404s on some entries; the surplus candidates from
    # discover() let extraction still deliver max_pages successful pages.
    sitemap = "<urlset>" + "".join(
        f"<url><loc>https://a.com/p{i}</loc></url>" for i in range(20)
    ) + "</urlset>"
    pages = {
        "https://a.com/robots.txt": "Sitemap: https://a.com/sitemap.xml",
        "https://a.com/sitemap.xml": sitemap,
        "https://a.com/": HOME,
    }
    # p0-p4 are stale (404); p5 onward resolve.
    for i in range(5, 20):
        pages[f"https://a.com/p{i}"] = f"<html><head><title>P{i}</title></head></html>"

    def backfill_handler(request):
        url = str(request.url)
        return httpx.Response(200, text=pages[url]) if url in pages else httpx.Response(404, text="")

    async with httpx.AsyncClient(transport=httpx.MockTransport(backfill_handler)) as client:
        result = await generate("https://a.com", client, max_pages=10)

    urls = [p["url"] for p in result["pages"] if p["url"] != "https://a.com/"]
    assert len(urls) == 10                      # failures backfilled from surplus
    assert "https://a.com/p14" in urls          # backfill reached deeper candidates
    assert "https://a.com/p15" not in urls      # stopped at max_pages successes


async def test_md_twins_swap_link_targets_when_verified():
    # /docs/start serves a real markdown twin; the homepage's "twin" is an SPA
    # shell (200 but text/html) and must not be trusted.
    requested = []

    def twin_handler(request):
        url = str(request.url)
        requested.append((request.method, url))
        if url == "https://a.com/docs/start.md":
            return httpx.Response(200, headers={"content-type": "text/markdown; charset=utf-8"})
        if url == "https://a.com/index.html.md":
            return httpx.Response(200, headers={"content-type": "text/html"})
        return httpx.Response(200, text=PAGES[url]) if url in PAGES else httpx.Response(404, text="")

    async with httpx.AsyncClient(transport=httpx.MockTransport(twin_handler)) as client:
        result = await generate("https://a.com", client)

    assert "(https://a.com/docs/start.md)" in result["llms_txt"]
    assert "https://a.com/index.html.md" not in result["llms_txt"]
    # Canonical URLs stay in the pages payload; the twin is carried separately.
    start = next(p for p in result["pages"] if p["url"] == "https://a.com/docs/start")
    assert start["md_url"] == "https://a.com/docs/start.md"
    # Twins are probed with HEAD, never downloaded.
    assert ("HEAD", "https://a.com/docs/start.md") in requested
    assert ("GET", "https://a.com/docs/start.md") not in requested


async def test_md_twin_probe_respects_robots_disallow():
    requested = []
    pages = dict(PAGES)
    pages["https://a.com/robots.txt"] = (
        "User-agent: *\nDisallow: /docs/start.md\nSitemap: https://a.com/sitemap.xml\n"
    )

    def twin_handler(request):
        url = str(request.url)
        requested.append(url)
        return httpx.Response(200, text=pages[url]) if url in pages else httpx.Response(404, text="")

    async with httpx.AsyncClient(transport=httpx.MockTransport(twin_handler)) as client:
        result = await generate("https://a.com", client)

    assert "https://a.com/docs/start.md" not in requested
    assert "(https://a.com/docs/start)" in result["llms_txt"]


async def test_honor_robots_false_generates_for_fully_disallowed_site():
    pages = dict(PAGES)
    pages["https://a.com/robots.txt"] = (
        "User-agent: *\nDisallow: /\nSitemap: https://a.com/sitemap.xml\n"
    )

    def blocked_handler(request):
        url = str(request.url)
        return httpx.Response(200, text=pages[url]) if url in pages else httpx.Response(404, text="")

    async with httpx.AsyncClient(transport=httpx.MockTransport(blocked_handler)) as client:
        result = await generate("https://a.com", client, honor_robots=False)

    urls = [p["url"] for p in result["pages"]]
    assert "https://a.com/" in urls
    assert "https://a.com/docs/start" in urls
    assert any("ignored" in warning for warning in result["warnings"])


async def test_honor_robots_false_still_uses_sitemap_hints():
    # Ignoring robots' restrictions must not throw away its useful part:
    # the Sitemap hint pointing somewhere other than /sitemap.xml.
    pages = {
        "https://a.com/robots.txt": (
            "User-agent: *\nDisallow: /\nSitemap: https://a.com/custom-sm.xml\n"
        ),
        "https://a.com/custom-sm.xml": SITEMAP,
        "https://a.com/": HOME,
        "https://a.com/docs/start": DOC,
    }

    def hint_handler(request):
        url = str(request.url)
        return httpx.Response(200, text=pages[url]) if url in pages else httpx.Response(404, text="")

    async with httpx.AsyncClient(transport=httpx.MockTransport(hint_handler)) as client:
        result = await generate("https://a.com", client, honor_robots=False)

    assert any(p["url"] == "https://a.com/docs/start" for p in result["pages"])


async def test_homepage_disallowed_by_robots_is_not_fetched():
    fetched = []

    def blocked_handler(request):
        url = str(request.url)
        fetched.append(url)
        if url == "https://a.com/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx.Response(200, text=HOME)

    async with httpx.AsyncClient(transport=httpx.MockTransport(blocked_handler)) as client:
        result = await generate("https://a.com", client)

    assert "https://a.com/" not in fetched
    assert result["pages"] == []
    assert any("robots" in warning for warning in result["warnings"])


async def test_crawl_delay_applies_between_page_fetches(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    pages = dict(PAGES)
    pages["https://a.com/robots.txt"] = (
        "User-agent: *\nCrawl-delay: 3\nSitemap: https://a.com/sitemap.xml\n"
    )

    def delay_handler(request):
        url = str(request.url)
        return httpx.Response(200, text=pages[url]) if url in pages else httpx.Response(404, text="")

    async with httpx.AsyncClient(transport=httpx.MockTransport(delay_handler)) as client:
        await generate("https://a.com", client)
    assert 3.0 in sleeps


async def test_crawl_delay_is_capped(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    pages = dict(PAGES)
    pages["https://a.com/robots.txt"] = (
        "User-agent: *\nCrawl-delay: 9999\nSitemap: https://a.com/sitemap.xml\n"
    )

    def delay_handler(request):
        url = str(request.url)
        return httpx.Response(200, text=pages[url]) if url in pages else httpx.Response(404, text="")

    async with httpx.AsyncClient(transport=httpx.MockTransport(delay_handler)) as client:
        await generate("https://a.com", client)
    assert 10.0 in sleeps
    assert 9999.0 not in sleeps


async def test_generate_enhance_applies_curation(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    curation = '{"summary": "AI summary", "pages": [{"url": "https://a.com/docs/start", "section": "Docs", "description": "ai desc"}]}'

    def enhance_handler(request):
        url = str(request.url)
        if "openrouter.ai" in url:
            return httpx.Response(200, json={"choices": [{"message": {"content": curation}}]})
        return httpx.Response(200, text=PAGES[url]) if url in PAGES else httpx.Response(404, text="")

    async with httpx.AsyncClient(transport=httpx.MockTransport(enhance_handler)) as client:
        result = await generate("https://a.com", client, enhance=True)
    assert "ai desc" in result["llms_txt"]


async def test_generate_bypass_escalates_blocked_fetch(monkeypatch):
    rendered = "<html><head><title>Rendered</title></head><body><a href='/p1'>p1</a></body></html>"

    class _Page:
        def __init__(self): self.url = "https://a.com/"
        async def goto(self, url, **kwargs):
            self.url = url
            return type("R", (), {"status": 200})()
        async def content(self): return rendered
        async def close(self): pass

    class _Browser:
        async def new_page(self): return _Page()

    @asynccontextmanager
    async def fake_session():
        yield _Browser()

    monkeypatch.setattr(generator_mod, "browser_session", fake_session)

    def blocked(request):
        return httpx.Response(403, text="blocked")

    async with httpx.AsyncClient(transport=httpx.MockTransport(blocked)) as client:
        result = await generate("https://a.com", client, bypass=True)

    # Direct fetch always 403 → every fetch escalates to the fake browser,
    # whose rendered title flows into the output.
    assert result["llms_txt"].startswith("# Rendered")

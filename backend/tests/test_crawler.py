import pytest

from fetcher import FetchResult
from crawler import crawl
from urls import normalize

pytestmark = pytest.mark.anyio


def _page_html(links):
    body = "".join(f'<a href="{href}">x</a>' for href in links)
    return f"<html><head><title>T</title></head><body>{body}</body></html>"


def _crawl_fetch(pages):
    async def fetch_fn(url):
        if url in pages:
            return FetchResult(url=url, status=200, text=pages[url], ok=True)
        return FetchResult(url=url, status=404, text="", ok=False)
    return fetch_fn


async def test_bfs_discovers_two_levels():
    pages = {
        "https://a.com/": _page_html(["/a"]),
        "https://a.com/a": _page_html(["/b"]),
        "https://a.com/b": _page_html([]),
    }
    results = await crawl("https://a.com", _crawl_fetch(pages), known=set(), disallow=[], delay=0)
    assert {r.url for r in results} == {"https://a.com/", "https://a.com/a", "https://a.com/b"}


async def test_bfs_respects_max_depth():
    pages = {
        "https://a.com/": _page_html(["/a"]),
        "https://a.com/a": _page_html(["/b"]),
        "https://a.com/b": _page_html([]),
    }
    results = await crawl("https://a.com", _crawl_fetch(pages), known=set(), disallow=[], max_depth=1, delay=0)
    urls = {r.url for r in results}
    assert "https://a.com/a" in urls
    assert "https://a.com/b" not in urls


async def test_bfs_no_infinite_loop_on_cycle():
    pages = {
        "https://a.com/": _page_html(["/a"]),
        "https://a.com/a": _page_html(["/"]),
    }
    results = await crawl("https://a.com", _crawl_fetch(pages), known=set(), disallow=[], delay=0)
    urls = [r.url for r in results]
    assert len(urls) == len(set(urls))


async def test_bfs_respects_limit():
    pages = {"https://a.com/": _page_html(["/a", "/b", "/c", "/d"])}
    for path in ["/a", "/b", "/c", "/d"]:
        pages[f"https://a.com{path}"] = _page_html([])
    results = await crawl("https://a.com", _crawl_fetch(pages), known=set(), disallow=[], limit=2, delay=0)
    assert len(results) == 2


async def test_bfs_skips_known_but_still_crawls_its_links():
    pages = {
        "https://a.com/": _page_html(["/a"]),
        "https://a.com/a": _page_html([]),
    }
    known = {normalize("https://a.com/")}
    results = await crawl("https://a.com", _crawl_fetch(pages), known=known, disallow=[], delay=0)
    urls = {r.url for r in results}
    assert "https://a.com/" not in urls
    assert "https://a.com/a" in urls


async def test_bfs_follows_document_relative_links():
    pages = {
        "https://a.com/docs/": '<html><body><a href="intro">i</a></body></html>',
        "https://a.com/docs/intro": "<title>Intro</title>",
    }
    results = await crawl("https://a.com/docs/", _crawl_fetch(pages), known=set(), disallow=[], delay=0)
    assert any(r.url == "https://a.com/docs/intro" for r in results)

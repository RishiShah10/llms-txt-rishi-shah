import pytest

from fetcher import FETCH_CONCURRENCY, FetchResult
from discoverer import discover, _parse_robots
from urls import is_disallowed

pytestmark = pytest.mark.anyio

SITEMAP = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://a.com/</loc><lastmod>2026-01-01</lastmod></url>
  <url><loc>https://a.com/docs</loc></url>
</urlset>"""

ROBOTS = "User-agent: *\nDisallow: /admin/\nSitemap: https://a.com/sitemap.xml\n"


def make_fetch(pages: dict[str, str]):
    async def fetch_fn(url: str) -> FetchResult:
        if url in pages:
            return FetchResult(url=url, status=200, text=pages[url], ok=True)
        return FetchResult(url=url, status=404, text="", ok=False)
    return fetch_fn


async def test_discovers_urls_from_sitemap():
    fetch_fn = make_fetch({
        "https://a.com/robots.txt": ROBOTS,
        "https://a.com/sitemap.xml": SITEMAP,
    })
    urls = [p.url for p in await discover("https://a.com", fetch_fn)]
    assert "https://a.com/" in urls
    assert "https://a.com/docs" in urls


async def test_bfs_fallback_without_sitemap():
    fetch_fn = make_fetch({
        "https://a.com/": '<a href="/about">About</a> <a href="https://a.com/docs">Docs</a>',
        "https://a.com/about": "<title>About</title>",
        "https://a.com/docs": "<title>Docs</title>",
    })
    urls = [p.url for p in await discover("https://a.com", fetch_fn)]
    assert "https://a.com/about" in urls
    assert "https://a.com/docs" in urls


async def test_returns_surplus_candidates_for_failure_backfill():
    # Sitemap entries can be stale (404 at extract time), so discover
    # over-collects to 2x max_pages; the generator stops at max_pages
    # *successes*, backfilling failures from the surplus.
    sitemap = "<urlset>" + "".join(
        f"<url><loc>https://a.com/p{i}</loc></url>" for i in range(100)
    ) + "</urlset>"
    fetch_fn = make_fetch({"https://a.com/sitemap.xml": sitemap})
    assert len(await discover("https://a.com", fetch_fn, max_pages=10)) == 20


async def test_max_pages_none_means_no_limit():
    sitemap = "<urlset>" + "".join(
        f"<url><loc>https://a.com/p{i}</loc></url>" for i in range(100)
    ) + "</urlset>"
    fetch_fn = make_fetch({"https://a.com/sitemap.xml": sitemap})
    assert len(await discover("https://a.com", fetch_fn, max_pages=None)) == 100


async def test_disallow_only_applies_to_wildcard_user_agent_group():
    # A block scoped to a specific bot (e.g. GPTBot) must not blind our
    # generic crawler; only the `User-agent: *` group's rules apply to us.
    robots = (
        "User-agent: GPTBot\n"
        "Disallow: /\n"
        "\n"
        "User-agent: *\n"
        "Disallow: /admin/\n"
        "Sitemap: https://a.com/sitemap.xml\n"
    )
    sitemap = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://a.com/</loc></url>
  <url><loc>https://a.com/docs</loc></url>
</urlset>"""
    fetch_fn = make_fetch({
        "https://a.com/robots.txt": robots,
        "https://a.com/sitemap.xml": sitemap,
    })
    urls = [p.url for p in await discover("https://a.com", fetch_fn)]
    assert "https://a.com/" in urls
    assert "https://a.com/docs" in urls

    rules = _parse_robots(robots)
    assert not is_disallowed("https://a.com/", rules.disallow)
    assert is_disallowed("https://a.com/admin/anything", rules.disallow)


async def test_bounds_sitemap_index_recursion_by_max_pages():
    nested_count = 20
    nested_urls = [f"https://a.com/sitemap-{i}.xml" for i in range(nested_count)]
    index = "<sitemapindex>" + "".join(
        f"<sitemap><loc>{url}</loc></sitemap>" for url in nested_urls
    ) + "</sitemapindex>"

    pages_per_nested = 5
    nested_sitemaps = {
        url: "<urlset>" + "".join(
            f"<url><loc>{url}#p{i}</loc></url>" for i in range(pages_per_nested)
        ) + "</urlset>"
        for url in nested_urls
    }

    call_count = {"n": 0}
    raw_fetch = make_fetch({"https://a.com/sitemap.xml": index, **nested_sitemaps})

    async def counting_fetch_fn(url: str):
        call_count["n"] += 1
        return await raw_fetch(url)

    max_pages = 8
    result = await discover("https://a.com", counting_fetch_fn, max_pages=max_pages)

    # Candidate collection is bounded by the surplus budget (2x max_pages).
    assert len(result) <= max_pages * 2

    # Pin the EXACT candidates, not just the count. discover() sorts the pool
    # before truncating it, so admitting surplus candidates changes WHICH pages
    # survive -- a bound expressed only as "fewer than all of them" cannot see
    # that. Sequentially, children 0..3 carry the raw pool past the 16-page
    # budget and recursion stops; each child's 5 entries differ only by fragment,
    # which normalize() strips, so one candidate per child survives the dedupe.
    assert [page.url for page in result] == [
        f"https://a.com/sitemap-{child}.xml#p0" for child in range(4)
    ]

    # robots.txt + top-level sitemap.xml + nested fetches. Children are fetched a
    # batch at a time, so the batch holding the child that crosses the budget is
    # fetched in full and its tail discarded -- bounded waste, and the reason this
    # is a `<=` and not an `== 4`. What must NOT happen is a second batch, or
    # those discarded children's pages reaching the candidate pool above.
    nested_fetches = call_count["n"] - 2
    assert nested_fetches <= FETCH_CONCURRENCY, (
        f"a second batch of nested sitemaps was fetched ({nested_fetches})"
    )
    assert nested_fetches < nested_count


def _crawl_fetch(pages):
    async def fetch_fn(url):
        if url in pages:
            return FetchResult(url=url, status=200, text=pages[url], ok=True)
        return FetchResult(url=url, status=404, text="", ok=False)
    return fetch_fn


def _flat_sitemap(paths):
    urls = "".join(f"<url><loc>https://a.com{p}</loc></url>" for p in paths)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'


async def test_sparse_sitemap_triggers_bfs_topup():
    pages = {
        "https://a.com/sitemap.xml": _flat_sitemap(["/known"]),
        "https://a.com/": '<a href="/extra">x</a>',
        "https://a.com/extra": "<title>Extra</title>",
    }
    urls = [p.url for p in await discover("https://a.com", _crawl_fetch(pages))]
    assert "https://a.com/known" in urls
    assert "https://a.com/extra" in urls


async def test_crawl_false_skips_bfs_topup():
    pages = {
        "https://a.com/sitemap.xml": _flat_sitemap(["/known"]),
        "https://a.com/": '<a href="/extra">x</a>',
        "https://a.com/extra": "<title>Extra</title>",
    }
    urls = [p.url for p in await discover("https://a.com", _crawl_fetch(pages), crawl=False)]
    assert "https://a.com/known" in urls
    assert "https://a.com/extra" not in urls


async def test_rich_sitemap_skips_bfs():
    # 12 sitemap pages against a budget of 20: comfortably above the relative
    # sparseness threshold (max_pages // 2), so no crawl is needed.
    pages = {
        "https://a.com/sitemap.xml": _flat_sitemap([f"/p{i}" for i in range(12)]),
        "https://a.com/": '<a href="/extra">x</a>',
        "https://a.com/extra": "<title>Extra</title>",
    }
    urls = [p.url for p in await discover("https://a.com", _crawl_fetch(pages), max_pages=20)]
    assert "https://a.com/extra" not in urls
    assert len(urls) == 12


async def test_sitemap_far_short_of_budget_triggers_bfs_topup():
    # A 12-page sitemap isn't "rich" when the user asked for 50 -- sparseness
    # is relative to the requested budget, not an absolute count.
    pages = {
        "https://a.com/sitemap.xml": _flat_sitemap([f"/p{i}" for i in range(12)]),
        "https://a.com/": '<a href="/extra">x</a>',
        "https://a.com/extra": "<title>Extra</title>",
    }
    urls = [p.url for p in await discover("https://a.com", _crawl_fetch(pages), max_pages=50)]
    assert "https://a.com/extra" in urls


async def test_probes_sitemap_index_when_sitemap_xml_missing():
    # WordPress/Yoast sites serve /sitemap_index.xml with nothing at
    # /sitemap.xml and often no Sitemap line in robots.txt.
    index = (
        "<sitemapindex><sitemap>"
        "<loc>https://a.com/sm-1.xml</loc>"
        "</sitemap></sitemapindex>"
    )
    fetch_fn = make_fetch({
        "https://a.com/sitemap_index.xml": index,
        "https://a.com/sm-1.xml": _flat_sitemap([f"/p{i}" for i in range(12)]),
    })
    urls = [p.url for p in await discover("https://a.com", fetch_fn)]
    assert "https://a.com/p0" in urls
    assert len(urls) == 12


async def test_sitemap_index_not_probed_when_sitemap_xml_works():
    calls = []
    raw = make_fetch({
        "https://a.com/sitemap.xml": _flat_sitemap([f"/p{i}" for i in range(12)]),
    })

    async def fetch_fn(url):
        calls.append(url)
        return await raw(url)

    await discover("https://a.com", fetch_fn)
    assert "https://a.com/sitemap_index.xml" not in calls


async def test_robots_allow_carves_exception_from_disallow():
    robots = (
        "User-agent: *\n"
        "Disallow: /private\n"
        "Allow: /private/ok\n"
        "Sitemap: https://a.com/sitemap.xml\n"
    )
    fetch_fn = make_fetch({
        "https://a.com/robots.txt": robots,
        "https://a.com/sitemap.xml": (
            "<urlset>"
            "<url><loc>https://a.com/private/ok</loc></url>"
            "<url><loc>https://a.com/private/no</loc></url>"
            "<url><loc>https://a.com/docs</loc></url>"
            "</urlset>"
        ),
    })
    urls = [p.url for p in await discover("https://a.com", fetch_fn, crawl=False)]
    assert "https://a.com/private/ok" in urls
    assert "https://a.com/private/no" not in urls
    assert "https://a.com/docs" in urls


async def test_sitemap_priority_wins_the_page_cap():
    # Low-priority entries appear first in document order, but the site says
    # the later ones matter more -- they must survive the cap.
    sitemap = "<urlset>" + "".join(
        f"<url><loc>https://a.com/low{i}</loc><priority>0.1</priority></url>"
        for i in range(4)
    ) + (
        "<url><loc>https://a.com/high-a</loc><priority>1.0</priority></url>"
        "<url><loc>https://a.com/high-b</loc><priority>0.9</priority></url>"
        "</urlset>"
    )
    fetch_fn = make_fetch({"https://a.com/sitemap.xml": sitemap})
    urls = [p.url for p in await discover("https://a.com", fetch_fn, max_pages=2, crawl=False)]
    assert urls[:2] == ["https://a.com/high-a", "https://a.com/high-b"]


async def test_document_order_kept_without_priority_or_lastmod():
    fetch_fn = make_fetch({
        "https://a.com/sitemap.xml": _flat_sitemap([f"/p{i}" for i in range(5)]),
    })
    urls = [p.url for p in await discover("https://a.com", fetch_fn, crawl=False)]
    assert urls == [f"https://a.com/p{i}" for i in range(5)]


async def test_newer_lastmod_breaks_priority_ties():
    sitemap = (
        "<urlset>"
        "<url><loc>https://a.com/old</loc><lastmod>2020-01-01</lastmod></url>"
        "<url><loc>https://a.com/new</loc><lastmod>2026-01-01</lastmod></url>"
        "</urlset>"
    )
    fetch_fn = make_fetch({"https://a.com/sitemap.xml": sitemap})
    urls = [p.url for p in await discover("https://a.com", fetch_fn, crawl=False)]
    assert urls == ["https://a.com/new", "https://a.com/old"]


async def test_sitemap_entries_off_origin_are_dropped():
    # A misconfigured or hostile sitemap must not inject foreign URLs.
    sitemap = (
        "<urlset>"
        "<url><loc>https://a.com/ok</loc></url>"
        "<url><loc>https://evil.com/x</loc></url>"
        "<url><loc>http://a.com/http-variant</loc></url>"
        "</urlset>"
    )
    fetch_fn = make_fetch({"https://a.com/sitemap.xml": sitemap})
    urls = [p.url for p in await discover("https://a.com", fetch_fn, crawl=False)]
    assert urls == ["https://a.com/ok"]


async def test_sitemap_asset_entries_are_dropped():
    sitemap = (
        "<urlset>"
        "<url><loc>https://a.com/whitepaper.pdf</loc></url>"
        "<url><loc>https://a.com/logo.png</loc></url>"
        "<url><loc>https://a.com/docs</loc></url>"
        "</urlset>"
    )
    fetch_fn = make_fetch({"https://a.com/sitemap.xml": sitemap})
    urls = [p.url for p in await discover("https://a.com", fetch_fn, crawl=False)]
    assert urls == ["https://a.com/docs"]


async def test_sitemap_slash_variants_are_deduped():
    sitemap = (
        "<urlset>"
        "<url><loc>https://a.com/docs</loc></url>"
        "<url><loc>https://a.com/docs/</loc></url>"
        "</urlset>"
    )
    fetch_fn = make_fetch({"https://a.com/sitemap.xml": sitemap})
    assert len(await discover("https://a.com", fetch_fn, crawl=False)) == 1


async def test_warns_when_no_usable_sitemap():
    warnings = []
    fetch_fn = make_fetch({})
    await discover("https://a.com", fetch_fn, crawl=False, warnings=warnings)
    assert any("sitemap" in warning for warning in warnings)


async def test_stacked_user_agent_lines_apply_to_all_listed_agents():
    # RFC 9309: a group may open with several User-agent lines; its rules
    # apply to every agent listed, regardless of the order of those lines.
    robots = (
        "User-agent: *\n"
        "User-agent: Googlebot\n"
        "Disallow: /admin/\n"
    )
    fetch_fn = make_fetch({
        "https://a.com/robots.txt": robots,
        "https://a.com/sitemap.xml": _flat_sitemap(
            ["/admin/secret", "/docs"] + [f"/p{i}" for i in range(10)]
        ),
    })
    urls = [p.url for p in await discover("https://a.com", fetch_fn)]
    assert "https://a.com/admin/secret" not in urls
    assert "https://a.com/docs" in urls


async def test_all_sitemap_hints_are_used():
    robots = (
        "User-agent: *\n"
        "Sitemap: https://a.com/sm-a.xml\n"
        "Sitemap: https://a.com/sm-b.xml\n"
    )
    fetch_fn = make_fetch({
        "https://a.com/robots.txt": robots,
        "https://a.com/sm-a.xml": _flat_sitemap([f"/a{i}" for i in range(6)]),
        "https://a.com/sm-b.xml": _flat_sitemap([f"/b{i}" for i in range(6)]),
    })
    urls = [p.url for p in await discover("https://a.com", fetch_fn)]
    assert "https://a.com/a0" in urls
    assert "https://a.com/b0" in urls


async def test_robots_5xx_treated_as_disallow_all():
    # RFC 9309 §2.3.1.4: if robots.txt is unreachable due to server errors,
    # assume complete disallow rather than crawling blind.
    async def fetch_fn(url):
        if url == "https://a.com/robots.txt":
            return FetchResult(url=url, status=503, text="", ok=False)
        if url == "https://a.com/sitemap.xml":
            return FetchResult(url=url, status=200, text=_flat_sitemap(["/p1", "/p2"]), ok=True)
        return FetchResult(url=url, status=404, text="", ok=False)

    assert await discover("https://a.com", fetch_fn) == []


async def test_crawl_delay_passed_to_bfs(monkeypatch):
    captured = {}

    async def fake_crawl(base_url, fetch_fn, known, disallow, limit=None, delay=None, **kwargs):
        captured["delay"] = delay
        return []

    monkeypatch.setattr("discoverer.bfs_crawl", fake_crawl)
    fetch_fn = make_fetch({"https://a.com/robots.txt": "User-agent: *\nCrawl-delay: 2\n"})
    await discover("https://a.com", fetch_fn)
    assert captured["delay"] == 2.0


async def test_crawl_delay_zero_when_robots_silent(monkeypatch):
    # Guards against re-introducing the 0.1 second floor. If a floor is added back
    # to line 142 of discoverer.py, delay will never be exactly 0.0, and the
    # upcoming concurrency gate (if delay > 0: go sequential) will silently serialize
    # all BFS crawls 100% of the time with no test failures. This test pins the floor removal.
    captured = {}

    async def fake_crawl(base_url, fetch_fn, known, disallow, limit=None, delay=None, **kwargs):
        captured["delay"] = delay
        return []

    monkeypatch.setattr("discoverer.bfs_crawl", fake_crawl)
    fetch_fn = make_fetch({"https://a.com/robots.txt": "User-agent: *\n"})
    await discover("https://a.com", fetch_fn)
    assert captured["delay"] == 0.0


async def test_no_limit_always_crawls():
    pages = {
        "https://a.com/sitemap.xml": _flat_sitemap([f"/p{i}" for i in range(12)]),
        "https://a.com/": '<a href="/extra">x</a>',
        "https://a.com/extra": "<title>Extra</title>",
    }
    for i in range(12):
        pages[f"https://a.com/p{i}"] = "<title>P</title>"
    urls = [p.url for p in await discover("https://a.com", _crawl_fetch(pages), max_pages=None)]
    assert "https://a.com/extra" in urls

"""Wasted-request regressions: every page must be fetched at most once, and no
request may be issued that is knowably a 404 before it is sent.

These pin the three defects a live progress log surfaced:
  1. a page in BOTH the sitemap and the BFS crawl was fetched twice -- BFS
     extracted it and threw the extraction away, so the generator refetched it.
  2. a page that IS markdown was probed for a `.md.md` twin -- a guaranteed 404.
  3. `/` and `/index.html` deduped to different keys, so the same page appeared
     twice in the document (and its twin was probed twice).
"""
from collections import Counter

import pytest

import generator
from fetcher import FetchResult
from urls import md_twin_of, normalize

pytestmark = pytest.mark.anyio

ORIGIN = "https://a.com"


def _html(path: str, links=()) -> str:
    anchors = "".join(f'<a href="{href}">{href}</a>' for href in links)
    return (
        f"<html><head><title>Title {path}</title>"
        f'<meta name="description" content="Desc {path}">'
        f"</head><body><h1>{path}</h1>{anchors}</body></html>"
    )


def _urlset(urls) -> str:
    items = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return (
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + items + "</urlset>"
    )


async def _run(monkeypatch, site, md_twins=(), redirects=None, **kwargs):
    """Drive the real pipeline against a synthetic site.

    `md_twins` are URLs a HEAD probe should answer as a real markdown mirror.
    `redirects` maps a requested URL to the URL the server lands it on, so a
    fetch can answer from a URL other than the one asked for -- the shape that
    tells a real redirect apart from a mere difference of spelling.

    Returns (result, fetch_counts, head_log)."""
    fetches = Counter()
    heads = []
    redirects = redirects or {}

    async def fake_fetch(url, client):
        fetches[url] += 1
        landed = redirects.get(url, url)
        if landed in site:
            return FetchResult(url=landed, status=200, text=site[landed], ok=True)
        return FetchResult(url=landed, status=404, text="", ok=False)

    async def fake_head(url, client):
        heads.append(url)
        if url in md_twins:
            return 200, "text/markdown; charset=utf-8"
        return 404, ""

    monkeypatch.setattr(generator, "fetch", fake_fetch)
    monkeypatch.setattr(generator, "head", fake_head)
    result = await generator.generate(ORIGIN, client=None, **kwargs)
    return result, fetches, heads


# --------------------------------------------------------------------------
# BUG 1: sitemap ∩ BFS pages were fetched twice.
# --------------------------------------------------------------------------

async def test_pages_in_both_sitemap_and_crawl_are_fetched_once(monkeypatch):
    overlap = [f"{ORIGIN}/docs/a", f"{ORIGIN}/docs/b"]
    crawl_only = [f"{ORIGIN}/docs/c"]
    site = {
        f"{ORIGIN}/robots.txt": f"User-agent: *\nSitemap: {ORIGIN}/sitemap.xml\n",
        f"{ORIGIN}/sitemap.xml": _urlset(overlap),
        f"{ORIGIN}/": _html("/", links=overlap + crawl_only),
    }
    for url in overlap + crawl_only:
        site[url] = _html(url)

    result, fetches, _ = await _run(monkeypatch, site, max_pages=10)

    # (The homepage is fetched once by generate() for the site title and once as
    # the BFS seed -- a separate, pre-existing cost, not the sitemap/BFS overlap.)
    repeated = {url: fetches[url] for url in overlap + crawl_only if fetches[url] > 1}
    assert not repeated, f"pages fetched more than once: {repeated}"
    # And the overlap pages are still fully extracted (title + description).
    by_url = {page["url"]: page for page in result["pages"]}
    for url in overlap + crawl_only:
        assert by_url[url]["title"] == f"Title {url}"
        assert by_url[url]["description"] == f"Desc {url}"


# --------------------------------------------------------------------------
# BUG 2: a markdown page has no markdown twin.
# --------------------------------------------------------------------------

def test_md_twin_of_a_markdown_page_is_none():
    assert md_twin_of("https://a.com/index.md") is None
    assert md_twin_of("https://a.com/docs/core.html.md") is None


async def test_markdown_pages_are_never_probed_for_a_md_md_twin(monkeypatch):
    pages = [f"{ORIGIN}/index.md", f"{ORIGIN}/docs/core.html.md", f"{ORIGIN}/docs/plain"]
    site = {
        f"{ORIGIN}/robots.txt": f"User-agent: *\nSitemap: {ORIGIN}/sitemap.xml\n",
        f"{ORIGIN}/sitemap.xml": _urlset(pages),
        f"{ORIGIN}/": _html("/"),
    }
    for url in pages:
        site[url] = _html(url)

    _, _, heads = await _run(monkeypatch, site, max_pages=10, crawl=False)

    assert not [url for url in heads if url.endswith(".md.md")], \
        f"probed a .md.md twin: {heads}"
    # The plain page is still probed -- the skip is surgical, not blanket.
    assert f"{ORIGIN}/docs/plain.md" in heads


# --------------------------------------------------------------------------
# BUG 3: `/` and `/index.html` are the same page.
# --------------------------------------------------------------------------

def test_normalize_treats_index_html_as_the_directory_root():
    assert normalize("https://a.com/index.html") == normalize("https://a.com/")
    assert normalize("https://a.com/index.htm") == normalize("https://a.com/")
    assert normalize("https://a.com/docs/index.html") == normalize("https://a.com/docs/")
    # Not every path that merely contains "index.html" is a root.
    assert normalize("https://a.com/myindex.html") == "https://a.com/myindex.html"


async def test_root_and_index_html_appear_once_in_the_document(monkeypatch):
    site = {
        f"{ORIGIN}/robots.txt": f"User-agent: *\nSitemap: {ORIGIN}/sitemap.xml\n",
        f"{ORIGIN}/sitemap.xml": _urlset([f"{ORIGIN}/index.html", f"{ORIGIN}/"]),
        f"{ORIGIN}/": _html("/"),
        f"{ORIGIN}/index.html": _html("/"),
    }

    result, _, heads = await _run(monkeypatch, site, max_pages=10, crawl=False)

    document = result["llms_txt"]
    assert document.count("- [") == 1, f"the homepage is listed twice:\n{document}"
    # The survivor keeps the sitemap entry's position AND its own spelling; the
    # duplicate is simply gone. Nothing else about the document moves.
    assert document == (
        "# Title /\n"
        "\n"
        "> Desc /\n"
        "\n"
        "## Pages\n"
        "- [Title /](https://a.com/index.html): Desc /\n"
    )
    # md_twin_of("/") and md_twin_of("/index.html") are the SAME URL -- the
    # duplicate page meant the same twin was probed twice.
    assert len(heads) == len(set(heads)), f"the same twin was probed twice: {heads}"


# --------------------------------------------------------------------------
# BUG 4: an HTML page and its .md twin both emit a link to the SAME URL.
# --------------------------------------------------------------------------

CORE_HTML = (
    "<html><head><title>core</title>"
    '<meta property="og:title" content="Python source – llms-txt">'
    '<meta name="description" content="Source code for llms_txt Python module">'
    "</head><body></body></html>"
)
CORE_MD = "# Python source\n\nThe llms.txt file spec is for files located in the path `llms.txt`.\n"


def _twin_pair_site():
    """A sitemap that lists BOTH representations of the same page, plus a
    twinless page after them (so ordering is observable)."""
    return {
        f"{ORIGIN}/robots.txt": f"User-agent: *\nSitemap: {ORIGIN}/sitemap.xml\n",
        f"{ORIGIN}/sitemap.xml": _urlset(
            [f"{ORIGIN}/core.html", f"{ORIGIN}/core.html.md", f"{ORIGIN}/later"]
        ),
        f"{ORIGIN}/": _html("/"),
        f"{ORIGIN}/core.html": CORE_HTML,
        f"{ORIGIN}/core.html.md": CORE_MD,
        f"{ORIGIN}/later": _html("/later"),
    }


async def test_html_page_and_its_md_twin_are_not_both_listed(monkeypatch):
    result, _, _ = await _run(
        monkeypatch, _twin_pair_site(),
        md_twins={f"{ORIGIN}/core.html.md"}, max_pages=10, crawl=False,
    )
    document = result["llms_txt"]

    # The HTML page links to the .md twin, and the standalone .md page links to
    # itself -- the SAME target, listed twice.
    assert document.count("core.html.md") == 1, f"the same URL is listed twice:\n{document}"

    # The survivor is the HTML page: it carries authored og:title/meta description,
    # while the markdown page's metadata is a scraped heading + first prose line.
    assert "- [Python source – llms-txt](https://a.com/core.html.md): " \
           "Source code for llms_txt Python module" in document
    assert "[Python source](" not in document
    assert "The llms.txt file spec is for files" not in document

    # The pages payload (also hashed by db.site_hash) agrees with the document.
    urls = [page["url"] for page in result["pages"]]
    assert urls.count(f"{ORIGIN}/core.html") == 1
    assert f"{ORIGIN}/core.html.md" not in urls


async def test_twin_dedupe_preserves_order(monkeypatch):
    result, _, _ = await _run(
        monkeypatch, _twin_pair_site(),
        md_twins={f"{ORIGIN}/core.html.md"}, max_pages=10, crawl=False,
    )
    document = result["llms_txt"]

    # ranker.rank sorts STABLY on a key that ties for every page here, so the
    # page list's order IS the document's link order. The dedupe filters in
    # place: the page listed after the pair still comes after it.
    assert document.index("core.html.md") < document.index("/later")
    assert [page["url"] for page in result["pages"]] == [
        f"{ORIGIN}/", f"{ORIGIN}/core.html", f"{ORIGIN}/later",
    ]


async def test_page_without_a_twin_is_untouched(monkeypatch):
    site = {
        f"{ORIGIN}/robots.txt": f"User-agent: *\nSitemap: {ORIGIN}/sitemap.xml\n",
        f"{ORIGIN}/sitemap.xml": _urlset([f"{ORIGIN}/solo"]),
        f"{ORIGIN}/": _html("/"),
        f"{ORIGIN}/solo": _html("/solo"),
    }
    # No md_twins: every probe 404s, so no page gets an md_url.
    result, _, _ = await _run(monkeypatch, site, max_pages=10, crawl=False)

    assert [page["url"] for page in result["pages"]] == [f"{ORIGIN}/", f"{ORIGIN}/solo"]
    assert f"({ORIGIN}/solo)" in result["llms_txt"]


# --------------------------------------------------------------------------
# BUG 5: keeping the BFS extraction must not lose the page's REDIRECT.
#
# Sparing the second fetch is only safe if the merged entry says what the
# refetch would have said -- including which URL the page finally lives at.
# A sitemap URL that 301s to its directory form has a markdown twin at
# /docs/index.html.md, not at /docs.md, so dropping the redirect silently
# downgrades the link from the markdown mirror to the HTML page.
# --------------------------------------------------------------------------

async def test_merged_page_keeps_the_redirect_target(monkeypatch):
    site = {
        f"{ORIGIN}/robots.txt": f"User-agent: *\nSitemap: {ORIGIN}/sitemap.xml\n",
        f"{ORIGIN}/sitemap.xml": _urlset([f"{ORIGIN}/docs"]),
        f"{ORIGIN}/": _html("/", links=[f"{ORIGIN}/docs"]),
        f"{ORIGIN}/docs/": _html("/docs/"),
    }
    result, fetches, _ = await _run(
        monkeypatch, site,
        redirects={f"{ORIGIN}/docs": f"{ORIGIN}/docs/"},
        md_twins={f"{ORIGIN}/docs/index.html.md"},
        max_pages=10,
    )

    # BFS requested exactly the sitemap's URL, so it followed the very redirect
    # a refetch would have followed: the page lives at the target. (The sitemap
    # entry keeps its POSITION; the homepage arrives from BFS, after it.)
    assert [page["url"] for page in result["pages"]] == [f"{ORIGIN}/docs/", f"{ORIGIN}/"]
    # ...which is what makes the markdown mirror findable at all.
    assert f"({ORIGIN}/docs/index.html.md)" in result["llms_txt"], result["llms_txt"]
    # And the whole point: the page still costs exactly one fetch.
    assert fetches[f"{ORIGIN}/docs"] == 1, dict(fetches)


async def test_merged_page_keeps_the_sitemaps_spelling(monkeypatch):
    """BFS reaching a page under another spelling is NOT a redirect."""
    site = {
        f"{ORIGIN}/robots.txt": f"User-agent: *\nSitemap: {ORIGIN}/sitemap.xml\n",
        # The sitemap says /docs/ ...
        f"{ORIGIN}/sitemap.xml": _urlset([f"{ORIGIN}/docs/"]),
        # ... but the homepage links to /docs, which the server serves directly.
        f"{ORIGIN}/": _html("/", links=[f"{ORIGIN}/docs"]),
        f"{ORIGIN}/docs": _html("/docs"),
    }
    result, fetches, _ = await _run(
        monkeypatch, site, md_twins={f"{ORIGIN}/docs/index.html.md"}, max_pages=10,
    )

    # Nobody ever asked the server for /docs/, so nothing licenses rewriting the
    # sitemap's link to the URL BFS happened to stumble onto.
    assert [page["url"] for page in result["pages"]] == [f"{ORIGIN}/docs/", f"{ORIGIN}/"]
    assert f"({ORIGIN}/docs/index.html.md)" in result["llms_txt"], result["llms_txt"]
    assert fetches[f"{ORIGIN}/docs"] == 1, dict(fetches)
    assert fetches[f"{ORIGIN}/docs/"] == 0, dict(fetches)


# --------------------------------------------------------------------------
# BUG 5: the homepage was fetched twice -- once by generate() for the site
# title/summary, once again as the BFS crawl's seed. Free before; with `bypass`
# on it is a *paid* browser render of the most expensive page, billed twice.
# --------------------------------------------------------------------------

async def test_homepage_is_fetched_once_not_twice(monkeypatch):
    site = {
        f"{ORIGIN}/": _html("/", links=[f"{ORIGIN}/one", f"{ORIGIN}/two"]),
        f"{ORIGIN}/one": _html("/one"),
        f"{ORIGIN}/two": _html("/two"),
    }
    result, fetches, _ = await _run(monkeypatch, site, max_pages=5, crawl=True)

    home = [url for url in fetches if url.rstrip("/") == ORIGIN]
    assert sum(fetches[url] for url in home) == 1, f"homepage fetched {fetches}"
    # The crawl must still actually work.
    assert f"{ORIGIN}/one" in result["llms_txt"]
    assert f"{ORIGIN}/two" in result["llms_txt"]


# --------------------------------------------------------------------------
# BUG 6: a bot wall answers 200 with a complete page, so a fully-walled crawl
# "succeeds" and ships a spec-compliant document made entirely of interstitials.
# pokemon.com did exactly this: 25 pages, every one titled "Pardon Our
# Interruption", uploaded to S3 without a word of complaint.
# --------------------------------------------------------------------------

_WALL = (
    "<html><head><title>Pardon Our Interruption</title></head><body>"
    "<h1>Pardon Our Interruption</h1><p>As you were browsing something about your "
    "browser made us think you were a bot.</p>"
    + "".join(f'<a href="/{i}">L{i}</a>' for i in range(60))
    + "</body></html>"
)


async def test_a_walled_crawl_warns_and_names_the_fix(monkeypatch):
    site = {
        f"{ORIGIN}/": _html("/", links=[f"{ORIGIN}/one"]),
        f"{ORIGIN}/one": _WALL,
        f"{ORIGIN}/two": _WALL,
    }
    result, _, _ = await _run(monkeypatch, site, max_pages=5, crawl=True)

    walled = [w for w in result["warnings"] if "bot-protection wall" in w]
    assert walled, f"a walled crawl shipped silently: {result['warnings']}"
    # The warning has to be actionable -- naming the toggle is the whole point.
    assert "Unblock protected sites" in walled[0]


async def test_an_unwalled_crawl_does_not_cry_wolf(monkeypatch):
    site = {
        f"{ORIGIN}/": _html("/", links=[f"{ORIGIN}/one"]),
        f"{ORIGIN}/one": _html("/one"),
    }
    result, _, _ = await _run(monkeypatch, site, max_pages=5, crawl=True)
    assert not [w for w in result["warnings"] if "bot-protection wall" in w]


async def test_the_wall_warning_has_no_nonsensical_denominator(monkeypatch):
    # `walled` counts walled REQUESTS -- discovery, the BFS walk, and surplus
    # candidates that never reach `pages`. Phrasing it as "N of len(pages)" once
    # produced the literal warning "17 of 8 pages were served a bot-protection
    # wall". Report a plain count instead.
    site = {f"{ORIGIN}/": _html("/", links=[f"{ORIGIN}/{i}" for i in range(6)])}
    site.update({f"{ORIGIN}/{i}": _WALL for i in range(6)})

    result, _, _ = await _run(monkeypatch, site, max_pages=2, crawl=True)

    walled = [w for w in result["warnings"] if "bot-protection wall" in w]
    assert walled
    assert " of " not in walled[0], f"nonsensical denominator: {walled[0]!r}"


# --------------------------------------------------------------------------
# BUG 7: a site that ALREADY publishes an llms.txt serves it as an ordinary
# page, so we crawled it and listed it inside the llms.txt we were generating --
# with its raw file contents as the description, since it has no meta tags.
# fastht.ml/docs did exactly this. Self-referential, and the ugliest line in the
# output.
# --------------------------------------------------------------------------

async def test_the_sites_own_llms_txt_is_not_listed_in_ours(monkeypatch):
    # Cover BOTH discovery paths: the sitemap (filtered in discover()) and the BFS
    # crawl (filtered in same_origin_links). An earlier version of this test only
    # exercised the crawl, so it passed even with the sitemap filter removed.
    listed = [
        f"{ORIGIN}/llms.txt", f"{ORIGIN}/llms-full.txt",
        f"{ORIGIN}/llms.html", f"{ORIGIN}/guide",
    ]
    site = {
        f"{ORIGIN}/sitemap.xml": _urlset(listed),
        f"{ORIGIN}/": _html("/", links=listed),
        f"{ORIGIN}/llms.txt": "# Site\n> Their existing llms.txt, raw.\n",
        f"{ORIGIN}/llms-full.txt": "# Site\n> The full one.\n",
        f"{ORIGIN}/llms.html": _html("/llms.html"),
        f"{ORIGIN}/guide": _html("/guide"),
    }
    result, fetches, _ = await _run(monkeypatch, site, max_pages=10, crawl=True)

    doc = result["llms_txt"]
    for name in ("llms.txt", "llms-full.txt", "llms.html"):
        assert f"{ORIGIN}/{name}" not in doc, f"we listed the site's own {name}"
        # And we should not have wasted a request fetching it, either.
        assert f"{ORIGIN}/{name}" not in fetches, f"fetched the site's own {name}"
    assert f"{ORIGIN}/guide" in doc


# --------------------------------------------------------------------------
# BUG 8: robots.txt and sitemaps were escalated to the PAID browser when a site
# 403s our datacenter IP. A browser can't help with them, and a Chromium-rendered
# sitemap stops being valid XML -- so we paid for the render AND lost the sitemap,
# then fell back to a BFS crawl we didn't need. resy.com: 3 of 4 paid renders,
# ~20s, for nothing.
# --------------------------------------------------------------------------

async def test_robots_and_sitemaps_are_never_sent_to_the_paid_browser(monkeypatch):
    from contextlib import asynccontextmanager
    from fetcher import FetchResult

    rendered: list[str] = []

    async def fake_browser_fetch(browser, url, warnings):
        rendered.append(url)
        warnings.append(f"used browser for {url}")
        return FetchResult(url=url, status=200, text=_html("/rendered"), ok=True)

    @asynccontextmanager
    async def fake_session():
        yield object()

    monkeypatch.setattr(generator, "browser_fetch", fake_browser_fetch)
    monkeypatch.setattr(generator, "browser_session", fake_session)

    # The site 403s everything -- exactly what resy does to an AWS IP.
    async def fake_fetch(url, client):
        return FetchResult(url=url, status=403, text="Forbidden", ok=False)

    async def fake_head(url, client):
        return 404, ""

    monkeypatch.setattr(generator, "fetch", fake_fetch)
    monkeypatch.setattr(generator, "head", fake_head)
    await generator.generate(ORIGIN, client=None, max_pages=5, crawl=True, bypass=True)

    for url in rendered:
        assert "robots.txt" not in url, f"paid to render robots.txt: {url}"
        assert not url.endswith(".xml"), f"paid to render a sitemap: {url}"


async def test_a_browser_render_is_an_event_not_a_warning(monkeypatch):
    # "used browser for X" in `warnings` made normal operation look like a fault --
    # a JS site produced one per page. It belongs on the progress event, where the
    # live log can mark the line, not in the warnings list.
    from contextlib import asynccontextmanager
    from fetcher import FetchResult
    from progress import Event

    async def fake_browser_fetch(browser, url, warnings):
        return FetchResult(url=url, status=200, text=_html("/rendered"), ok=True)

    @asynccontextmanager
    async def fake_session():
        yield object()

    # Every page is a JS shell -> escalates.
    shell = "<!DOCTYPE html><html><body><div id='root'></div></body></html>"

    async def fake_fetch(url, client):
        return FetchResult(url=url, status=200, text=shell, ok=True)

    async def fake_head(url, client):
        return 404, ""

    monkeypatch.setattr(generator, "browser_fetch", fake_browser_fetch)
    monkeypatch.setattr(generator, "browser_session", fake_session)
    monkeypatch.setattr(generator, "fetch", fake_fetch)
    monkeypatch.setattr(generator, "head", fake_head)

    events: list[Event] = []
    result = await generator.generate(ORIGIN, client=None, max_pages=3, crawl=False,
                                      bypass=True, on_event=events.append)

    assert not [w for w in result["warnings"] if "used browser" in w], \
        f"a successful render should not warn: {result['warnings']}"
    assert [e for e in events if e.browser], "the render was not reported on any event"


# --------------------------------------------------------------------------
# BUG 9 + 10: description quality, both seen on resy.com.
#   9. A page with no <meta description> falls back to og:description, which is
#      set once site-wide -- so Privacy, Terms and Careers were all described as
#      "Discover restaurants to love in your city and beyond...".
#  10. /join/get-started/ was listed FIVE times, differing only by a
#      ?desired_plan= that preselects a plan on the same form. Same title, same
#      description, five identical rows.
# --------------------------------------------------------------------------

def _page(path: str, title: str, desc: str) -> str:
    return (f'<html><head><title>{title}</title>'
            f'<meta property="og:description" content="{desc}">'
            f"</head><body><h1>{path}</h1></body></html>")


async def test_a_page_does_not_inherit_the_sites_description(monkeypatch):
    site_blurb = "Discover restaurants to love in your city and beyond."
    site = {
        f"{ORIGIN}/": _page("/", "Resy", site_blurb),
        f"{ORIGIN}/sitemap.xml": _urlset([f"{ORIGIN}/", f"{ORIGIN}/privacy"]),
        # No meta description of its own -> og:description -> the SITE's blurb.
        f"{ORIGIN}/privacy": _page("/privacy", "Global Privacy Policy", site_blurb),
    }
    result, _, _ = await _run(monkeypatch, site, max_pages=5, crawl=False)

    doc = result["llms_txt"]
    assert f"> {site_blurb}" in doc, "the homepage keeps it -- there it is true"
    privacy = [ln for ln in doc.splitlines() if "/privacy" in ln][0]
    assert site_blurb not in privacy, f"Privacy inherited the site blurb: {privacy}"


async def test_identical_entries_are_listed_once(monkeypatch):
    desc = "Book a demo with Resy to find the plan that works for your business."
    variants = [f"{ORIGIN}/join/get-started/"] + [
        f"{ORIGIN}/join/get-started/?desired_plan={p}"
        for p in ("platform", "platform-360", "essential", "premium")
    ]
    site = {f"{ORIGIN}/": _page("/", "Resy", "A site."),
            f"{ORIGIN}/sitemap.xml": _urlset(variants)}
    for url in variants:
        site[url] = _page("/join/get-started/", "Get Started With A Demo", desc)

    result, _, _ = await _run(monkeypatch, site, max_pages=10, crawl=False)

    listed = [ln for ln in result["llms_txt"].splitlines() if "get-started" in ln]
    assert len(listed) == 1, f"{len(listed)} identical rows:\n" + "\n".join(listed)


async def test_pages_without_descriptions_are_not_collapsed_by_title(monkeypatch):
    # Two real pages can share a title and have no description. Deduping on title
    # alone would silently lose one.
    site = {
        f"{ORIGIN}/": _html("/"),
        f"{ORIGIN}/sitemap.xml": _urlset([f"{ORIGIN}/a", f"{ORIGIN}/b"]),
        f"{ORIGIN}/a": "<html><head><title>Docs</title></head><body></body></html>",
        f"{ORIGIN}/b": "<html><head><title>Docs</title></head><body></body></html>",
    }
    result, _, _ = await _run(monkeypatch, site, max_pages=5, crawl=False)
    doc = result["llms_txt"]
    assert f"{ORIGIN}/a" in doc and f"{ORIGIN}/b" in doc, "a real page was collapsed away"


# --------------------------------------------------------------------------
# BUG 11: `limit` bounded pages COLLECTED, not work DONE. A failed fetch costs a
# request but never advances the counter, so a site that 403s everything made BFS
# drain the ENTIRE frontier. sephora.com 403s every /brand/* page and its
# /brands-list hub injects hundreds of them -- so we ground through the whole
# alphabetical catalogue, and with bypass on, every 403 fired a PAID browser render
# attempt that then failed, each able to burn the full 45s nav timeout.
# --------------------------------------------------------------------------

async def test_a_site_that_refuses_everything_stops_the_crawl(monkeypatch):
    from crawler import MIN_ATTEMPTS_BEFORE_GIVING_UP
    from fetcher import FetchResult

    # A hub page linking to 300 brands, exactly like sephora's /brands-list.
    hub = "".join(f'<a href="{ORIGIN}/brand/{i}">b{i}</a>' for i in range(300))
    fetched: list[str] = []

    async def fake_fetch(url, client):
        fetched.append(url)
        if url.rstrip("/") == ORIGIN:
            return FetchResult(url=url, status=200,
                               text=f"<html><body>{hub}</body></html>", ok=True)
        # Everything else refuses us, like sephora does.
        return FetchResult(url=url, status=403, text="Forbidden", ok=False)

    async def fake_head(url, client):
        return 404, ""

    monkeypatch.setattr(generator, "fetch", fake_fetch)
    monkeypatch.setattr(generator, "head", fake_head)
    await generator.generate(ORIGIN, client=None, max_pages=50, crawl=True)

    brand_fetches = [u for u in fetched if "/brand/" in u]
    assert len(brand_fetches) < 60, (
        f"ground through {len(brand_fetches)} refusals; the site said no "
        f"{MIN_ATTEMPTS_BEFORE_GIVING_UP} times and we kept asking"
    )


async def test_a_healthy_crawl_is_not_cut_short(monkeypatch):
    # The give-up rule must not fire on a site that merely has a few dead links.
    links = "".join(f'<a href="{ORIGIN}/p{i}">p{i}</a>' for i in range(30))
    site = {f"{ORIGIN}/": _html("/", links=[f"{ORIGIN}/p{i}" for i in range(30)])}
    for i in range(30):
        if i % 10 != 0:                      # ~10% dead -- well under the threshold
            site[f"{ORIGIN}/p{i}"] = _html(f"/p{i}")

    result, _, _ = await _run(monkeypatch, site, max_pages=20, crawl=True)
    assert len(result["pages"]) >= 15, f"only {len(result['pages'])} pages -- cut short"

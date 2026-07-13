"""Output parity: concurrency must change only SPEED, never the document.

Every expectation below is a golden captured from the pre-async implementation
(85f6191, sequential). They are not descriptions of what the code currently does
-- they are the contract the async rewrite exists to preserve. If a change here
turns one red, the async pipeline has started emitting a different llms.txt than
the sequential one did for the same site, which is a regression by definition.

Two shapes are load-bearing regression guards:

  * `wide_index`  -- a sitemap index with more children than FETCH_CONCURRENCY.
    discover() SORTS the candidate pool before truncating it to the budget, so a
    batched early-exit that admits a whole batch's surplus candidates changes
    WHICH pages win the sort, not merely how many were considered.

  * `cross_ref`   -- index A -> [B, C] where B is itself an index listing C.
    Claiming a batch's URLs in `seen` before recursing lets A steal C from B's
    subtree, which yields the same page SET in a different ORDER. db.site_hash
    sorts before hashing and so CANNOT see this: /check-changes would report
    "no changes" while the stored document silently differs. Pinning order is
    the only thing that catches it.
"""
import re

import pytest

import generator
from fetcher import FetchResult

pytestmark = pytest.mark.anyio

ORIGIN = "https://a.com"


def _html(path: str, links=()) -> str:
    anchors = "".join(f'<a href="{href}">{href}</a>' for href in links)
    return (
        f"<html><head><title>Title {path}</title>"
        f'<meta name="description" content="Desc {path}">'
        f"</head><body><h1>{path}</h1>"
        f"<p>Body text for {path} with enough words to look like a real page.</p>"
        f"{anchors}</body></html>"
    )


def _urlset(entries, priority=None) -> str:
    items = []
    for url, lastmod in entries:
        item = f"<url><loc>{url}</loc>"
        if lastmod:
            item += f"<lastmod>{lastmod}</lastmod>"
        if priority is not None:
            item += f"<priority>{priority}</priority>"
        items.append(item + "</url>")
    return (
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(items) + "</urlset>"
    )


def _index(child_urls) -> str:
    return (
        '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in child_urls)
        + "</sitemapindex>"
    )


def _robots(*sitemaps) -> str:
    return "\n".join(["User-agent: *", *(f"Sitemap: {s}" for s in sitemaps)]) + "\n"


def _add_pages(site, urls):
    for url in urls:
        site[url] = _html(url)


async def _run(monkeypatch, site, **kwargs):
    """Drive the real pipeline against a synthetic site; return (result, fetch_log)."""
    fetch_log = []

    async def fake_fetch(url, client):
        fetch_log.append(url)
        if url in site:
            return FetchResult(url=url, status=200, text=site[url], ok=True)
        return FetchResult(url=url, status=404, text="", ok=False)

    async def fake_head(url, client):
        return 404, ""  # no .md twins: keeps the pinned links plain

    monkeypatch.setattr(generator, "fetch", fake_fetch)
    monkeypatch.setattr(generator, "head", fake_head)
    return await generator.generate(ORIGIN, client=None, **kwargs), fetch_log


def _links(document: str) -> list[str]:
    return re.findall(r"\]\((https://a\.com[^)]*)\)", document)


# --------------------------------------------------------------------------
# BLOCKER 1: batched early-exit must not widen the candidate pool.
# --------------------------------------------------------------------------

def _wide_index_site():
    """15 children x 40 pages; child 7 alone carries a newer lastmod."""
    site = {f"{ORIGIN}/": _html("/")}
    children = [f"{ORIGIN}/sm-c{c}.xml" for c in range(15)]
    site[f"{ORIGIN}/robots.txt"] = _robots(f"{ORIGIN}/sitemap_index.xml")
    site[f"{ORIGIN}/sitemap_index.xml"] = _index(children)
    for c, child in enumerate(children):
        lastmod = "2025-06-01" if c == 7 else "2020-01-01"
        urls = [f"{ORIGIN}/c{c}-p{p}" for p in range(40)]
        site[child] = _urlset([(u, lastmod) for u in urls])
        _add_pages(site, urls)
    return site


async def test_wide_sitemap_index_pins_page_order_at_the_budget(monkeypatch):
    result, fetch_log = await _run(monkeypatch, _wide_index_site(), max_pages=50, crawl=False)

    # Budget is 50, surplus budget 100. Sequentially, children 0/1/2 carry the
    # pool past 100 and recursion stops there -- child 7 is NEVER fetched, so its
    # newer lastmod never competes in the sort.
    expected = (
        [f"{ORIGIN}/"]
        + [f"{ORIGIN}/c0-p{i}" for i in range(40)]
        + [f"{ORIGIN}/c1-p{i}" for i in range(10)]
    )
    assert _links(result["llms_txt"]) == expected

    # The regression this pins: batching admitted 10 children's pages into the
    # pool, child 7's newer lastmod won the sort, and the document led with c7-p0.
    assert not any("/c7-" in link for link in _links(result["llms_txt"])), \
        "child 7 was never fetched sequentially; it must not reach the output"

    # Stop at the child that crosses the budget. The rest of that child's batch is
    # already in flight and gets discarded -- bounded waste -- but no further
    # BATCH may be issued, so the sitemap fetches stay well under all 15.
    child_fetches = [u for u in fetch_log if "/sm-c" in u]
    assert len(child_fetches) <= 10, "a second batch of children was fetched"


async def test_unbounded_wide_index_fetches_every_child(monkeypatch):
    # max_pages=None means no early exit at all: the whole index is walked, so
    # child 7's newer lastmod legitimately DOES win the sort and leads the doc.
    result, _ = await _run(monkeypatch, _wide_index_site(), max_pages=None, crawl=False)
    links = _links(result["llms_txt"])

    assert links[:2] == [f"{ORIGIN}/", f"{ORIGIN}/c7-p0"]
    assert len(links) == 601  # homepage + 15 children x 40 pages
    assert links[41:43] == [f"{ORIGIN}/c0-p0", f"{ORIGIN}/c0-p1"]


# --------------------------------------------------------------------------
# BLOCKER 2: a child's own subtree gets first claim on a cross-referenced URL.
# --------------------------------------------------------------------------

def _cross_ref_site():
    """A -> [B, C]; B is itself an index listing [C, D]. C belongs to B's subtree."""
    site = {f"{ORIGIN}/": _html("/")}
    a, b, c, d = (f"{ORIGIN}/sm-{n}.xml" for n in "abcd")
    site[f"{ORIGIN}/robots.txt"] = _robots(a)
    site[a] = _index([b, c])
    site[b] = _index([c, d])
    site[c] = _urlset([(f"{ORIGIN}/page-c{i}", None) for i in range(3)])
    site[d] = _urlset([(f"{ORIGIN}/page-d{i}", None) for i in range(3)])
    _add_pages(site, [f"{ORIGIN}/page-c{i}" for i in range(3)])
    _add_pages(site, [f"{ORIGIN}/page-d{i}" for i in range(3)])
    return site


CROSS_REF_DOCUMENT = (
    "# Title /\n"
    "\n"
    "> Desc /\n"
    "\n"
    "## Pages\n"
    "- [Title /](https://a.com/): Desc /\n"
    "- [Title https://a.com/page-c0](https://a.com/page-c0): Desc https://a.com/page-c0\n"
    "- [Title https://a.com/page-c1](https://a.com/page-c1): Desc https://a.com/page-c1\n"
    "- [Title https://a.com/page-c2](https://a.com/page-c2): Desc https://a.com/page-c2\n"
    "- [Title https://a.com/page-d0](https://a.com/page-d0): Desc https://a.com/page-d0\n"
    "- [Title https://a.com/page-d1](https://a.com/page-d1): Desc https://a.com/page-d1\n"
    "- [Title https://a.com/page-d2](https://a.com/page-d2): Desc https://a.com/page-d2\n"
)


async def test_cross_referenced_index_pins_exact_document(monkeypatch):
    result, fetch_log = await _run(monkeypatch, _cross_ref_site(), max_pages=20, crawl=False)

    # C is consumed INSIDE B's subtree, so page-c precedes page-d. Pre-claiming
    # C for A flips these two groups -- same set, same hash, different document.
    assert result["llms_txt"] == CROSS_REF_DOCUMENT

    # Cross-referenced or not, a sitemap URL is fetched exactly once.
    sitemap_fetches = [u for u in fetch_log if u.endswith(".xml")]
    assert len(sitemap_fetches) == len(set(sitemap_fetches)), \
        f"a sitemap was fetched twice: {sitemap_fetches}"


async def test_cyclic_sitemap_index_terminates(monkeypatch):
    """Three levels deep, with self- and back-references. `seen` must still bound it."""
    site = {f"{ORIGIN}/": _html("/")}
    root = f"{ORIGIN}/sitemap_index.xml"
    site[f"{ORIGIN}/robots.txt"] = _robots(root)
    mids = [f"{ORIGIN}/mid-{i}.xml" for i in range(3)]
    site[root] = _index(mids)
    for i, mid in enumerate(mids):
        leaves = [f"{ORIGIN}/leaf-{i}-{j}.xml" for j in range(3)]
        site[mid] = _index(leaves + [mid, root])  # self-ref + back-ref to root
        for j, leaf in enumerate(leaves):
            urls = [f"{ORIGIN}/l{i}{j}-p{k}" for k in range(4)]
            # An explicit priority outranks the homepage's absent one, which is
            # why the homepage lands LAST here rather than first.
            site[leaf] = _urlset([(u, f"2023-0{j + 1}-01") for u in urls], priority=0.5)
            _add_pages(site, urls)

    result, fetch_log = await _run(monkeypatch, site, max_pages=25, crawl=False)

    # Newest lastmod first (leaf j=2), then j=1, then the one j=0 page that fits.
    expected = [
        f"{ORIGIN}/l{i}{j}-p{k}"
        for j in (2, 1)
        for i in range(3)
        for k in range(4)
    ] + [f"{ORIGIN}/l00-p0", f"{ORIGIN}/"]
    assert _links(result["llms_txt"]) == expected

    sitemap_fetches = [u for u in fetch_log if u.endswith(".xml")]
    assert len(sitemap_fetches) == len(set(sitemap_fetches)), "a cycle caused a refetch"


async def test_children_that_404_are_skipped_without_shifting_order(monkeypatch):
    site = {f"{ORIGIN}/": _html("/")}
    children = [f"{ORIGIN}/sm-{i}.xml" for i in range(12)]
    site[f"{ORIGIN}/robots.txt"] = _robots(f"{ORIGIN}/sitemap_index.xml")
    site[f"{ORIGIN}/sitemap_index.xml"] = _index(children)
    for i, child in enumerate(children):
        if i % 3 == 1:
            continue  # this child 404s
        urls = [f"{ORIGIN}/s{i}-p{p}" for p in range(5)]
        site[child] = _urlset([(u, None) for u in urls])
        _add_pages(site, urls)
        del site[f"{ORIGIN}/s{i}-p0"]  # and this page 404s at extract time

    result, _ = await _run(monkeypatch, site, max_pages=15, crawl=False)

    # Dead children contribute nothing; dead pages backfill from the surplus.
    expected = [f"{ORIGIN}/"] + [
        f"{ORIGIN}/s{i}-p{p}" for i in (0, 2, 3, 5) for p in range(1, 5)
    ]
    assert _links(result["llms_txt"]) == expected[:16]


async def test_flat_sitemap_pins_lastmod_ordering(monkeypatch):
    site = {f"{ORIGIN}/": _html("/")}  # robots.txt 404s -> /sitemap.xml probe path
    urls = [f"{ORIGIN}/p{i}" for i in range(30)]
    site[f"{ORIGIN}/sitemap.xml"] = _urlset(
        [(u, f"2024-01-{(i % 28) + 1:02d}") for i, u in enumerate(urls)]
    )
    _add_pages(site, urls)

    result, _ = await _run(monkeypatch, site, max_pages=20, crawl=False)

    # Newest lastmod first: p27 (2024-01-28) down to p8.
    expected = [f"{ORIGIN}/"] + [f"{ORIGIN}/p{i}" for i in range(27, 7, -1)]
    assert _links(result["llms_txt"]) == expected

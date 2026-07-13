import asyncio
from dataclasses import dataclass, field

from lxml import etree

# `discover()` has its own `crawl: bool` parameter, which would shadow a
# bare `crawl` import inside that function's scope -- alias to the BFS
# module's name instead of the ambiguous bare form.
from crawler import Harvest
from crawler import crawl as bfs_crawl
from fetcher import FETCH_CONCURRENCY, FetchResult, as_fetch_result
from models import PageInfo
from urls import in_scope, is_asset, is_disallowed, is_llms_file, normalize, origin_of

SPARSE_SITEMAP_THRESHOLD = 10
# Sitemap entries are unverified and may 404 at extract time. Collecting a
# surplus of candidates costs nothing (no page fetches), and lets the
# generator backfill failures so the output still reaches max_pages.
CANDIDATE_SURPLUS = 2
# A hostile or misconfigured Crawl-delay (some sites say 3600) would stall a
# synchronous request for hours; real crawlers cap it, and so do we.
MAX_CRAWL_DELAY = 10.0


@dataclass
class RobotsRules:
    disallow: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    crawl_delay: float | None = None


def _parse_robots(text: str) -> RobotsRules:
    # Disallow/Crawl-delay rules are scoped to the User-agent group they appear
    # under. We're a generic crawler, so only groups naming `*` apply to us --
    # a block aimed at e.g. GPTBot must not blind our own crawling. A group may
    # open with several stacked User-agent lines; its rules apply to all of them.
    rules = RobotsRules()
    group_agents: set[str] = set()
    reading_group_header = False
    applies = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if not reading_group_header:
                group_agents = set()
                reading_group_header = True
            group_agents.add(value.lower())
            applies = "*" in group_agents
            continue
        reading_group_header = False
        if key == "disallow" and applies and value:
            rules.disallow.append(value)
        elif key == "allow" and applies and value:
            rules.allow.append(value)
        elif key == "crawl-delay" and applies:
            try:
                rules.crawl_delay = min(max(float(value), 0.0), MAX_CRAWL_DELAY)
            except ValueError:
                pass
        elif key == "sitemap" and value:
            # Sitemap lines are global (not group-scoped) and there can be many.
            rules.sitemaps.append(value)
    return rules


async def read_robots(origin: str, fetch_fn, warnings: list[str] | None = None) -> RobotsRules:
    response = await fetch_fn(f"{origin}/robots.txt")
    if response.ok:
        return _parse_robots(response.text)
    if response.status >= 500:
        # RFC 9309 §2.3.1.4: robots.txt unreachable due to server errors means
        # assume complete disallow rather than crawling blind.
        if warnings is not None:
            warnings.append(
                f"robots.txt returned status {response.status} — "
                "treating the whole site as disallowed"
            )
        return RobotsRules(disallow=["/"])
    # Missing robots.txt (404 etc.) conventionally means no restrictions.
    return RobotsRules()


def _page_from_url_element(element) -> PageInfo | None:
    loc = element.find("{*}loc")
    if loc is None or not (loc.text or "").strip():
        return None
    lastmod = element.find("{*}lastmod")
    priority = element.find("{*}priority")
    return PageInfo(
        url=loc.text.strip(),
        title="",
        lastmod=lastmod.text.strip() if lastmod is not None and lastmod.text else None,
        priority=float(priority.text) if priority is not None and priority.text else None,
    )


async def _parse_sitemap(
    text: str, fetch_fn, seen: set[str], max_pages: int | None, delay: float = 0.0,
    fetched: dict[str, FetchResult] | None = None,
) -> list[PageInfo]:
    try:
        root = etree.fromstring(text.encode("utf-8"))
    except etree.XMLSyntaxError:
        return []

    if etree.QName(root).localname == "sitemapindex":
        # Fetch concurrently, but CLAIM (add to `seen`) and recurse in document
        # order: claiming a child only when consumed keeps a cross-referenced index
        # in the order the sequential version produced -- which matters because the
        # page ORDER decides the sort downstream, and site_hash can't see reorders.
        # `fetched` memoizes so a child claimed by two subtrees costs one request.
        if fetched is None:
            fetched = {}
        nested_urls = [
            nested_url for loc in root.iter("{*}loc")
            if (nested_url := (loc.text or "").strip())
        ]

        pages: list[PageInfo] = []
        width = 1 if delay > 0 else max(1, FETCH_CONCURRENCY)
        for start in range(0, len(nested_urls), width):
            batch = nested_urls[start:start + width]

            # Prefetch this window concurrently. Skip anything already claimed by
            # an earlier sibling's subtree (it will never be fetched here) or
            # already memoized. `dict.fromkeys` dedupes an index that
            # lists the same child twice without disturbing document order.
            todo = list(dict.fromkeys(
                url for url in batch if url not in seen and url not in fetched
            ))
            if todo:
                if delay > 0:
                    for url in todo:
                        fetched[url] = await fetch_fn(url)
                        await asyncio.sleep(delay)
                else:
                    responses = await asyncio.gather(
                        *(fetch_fn(url) for url in todo), return_exceptions=True
                    )
                    for url, response in zip(todo, responses):
                        fetched[url] = as_fetch_result(url, response)

            for url in batch:
                if url in seen:
                    continue
                seen.add(url)
                response = fetched.get(url)
                if response is not None and response.ok:
                    pages.extend(
                        await _parse_sitemap(
                            response.text, fetch_fn, seen, max_pages, delay, fetched
                        )
                    )
                # Stop once we have enough, checked after EVERY child (not per
                # batch): discover() sorts then truncates, so admitting a batch's
                # surplus would change WHICH pages win. The rest of the fetched
                # batch is discarded -- bounded waste, the price of batching.
                if max_pages is not None and len(pages) >= max_pages:
                    return pages
        return pages

    return [page for element in root.iter("{*}url")
            if (page := _page_from_url_element(element)) is not None]


async def discover(
    base_url: str,
    fetch_fn,
    max_pages: int | None = 50,
    crawl: bool = True,
    scope_prefix: str | None = None,
    robots: RobotsRules | None = None,
    warnings: list[str] | None = None,
) -> list[PageInfo]:
    origin = origin_of(base_url)
    rules = robots if robots is not None else await read_robots(origin, fetch_fn)
    delay = rules.crawl_delay or 0.0
    budget = None if max_pages is None else max_pages * CANDIDATE_SURPLUS

    pages: list[PageInfo] = []
    # WordPress/Yoast sites serve /sitemap_index.xml with nothing at
    # /sitemap.xml, often without a Sitemap line in robots.txt.
    probing = not rules.sitemaps
    sitemap_urls = rules.sitemaps or [f"{origin}/sitemap.xml", f"{origin}/sitemap_index.xml"]
    seen_sitemaps = set(sitemap_urls)
    for sitemap_url in sitemap_urls:
        sitemap = await fetch_fn(sitemap_url)
        if sitemap.ok:
            pages.extend(await _parse_sitemap(sitemap.text, fetch_fn, seen_sitemaps, budget, delay))
            # Fallback probes are alternatives, not a set: robots.txt hints
            # must all be read, but the first probe that yields pages wins.
            if probing and pages:
                break
        if budget is not None and len(pages) >= budget:
            break
    # robots.txt/sitemap.xml live at the origin root, but the requested scope may
    # be a subpath -- keep only sitemap entries that fall within it. Sitemaps are
    # trusted for discovery, not for content: a misconfigured or hostile one must
    # not inject foreign origins or binary assets into the output.
    pages = [
        page for page in pages
        if in_scope(page.url, scope_prefix)
        and origin_of(page.url) == origin
        and not is_asset(page.url)
        # A site that already has an llms.txt should not have it listed inside
        # the llms.txt we generate for it.
        and not is_llms_file(page.url)
    ]
    if not pages and warnings is not None:
        # Distinguish "sitemap failed/absent" from "site has no pages" -- silent
        # degradation to crawling is otherwise invisible to the caller.
        warnings.append("no usable sitemap entries — discovery relies on crawling")

    # When the site declares importance, honor it before the page cap: priority
    # first (0.5 is the sitemap spec's default for absent values), newest
    # lastmod as tiebreak. Both sorts are stable, so sitemaps without these
    # fields keep their document order untouched.
    pages.sort(key=lambda page: page.lastmod or "", reverse=True)
    pages.sort(
        key=lambda page: page.priority if page.priority is not None else 0.5,
        reverse=True,
    )

    no_limit = max_pages is None
    remaining = None if no_limit else max_pages - len(pages)
    # Sparseness is relative to the requested budget: a 15-page sitemap is
    # rich for max_pages=20 but far short of max_pages=100.
    threshold = (
        SPARSE_SITEMAP_THRESHOLD if no_limit
        else max(SPARSE_SITEMAP_THRESHOLD, max_pages // 2)
    )
    sparse = len(pages) < threshold
    # Pages the BFS crawl fetched that the sitemap already listed. BFS extracts
    # them anyway, so keeping the extraction spares the generator a second fetch
    # of a page we have already downloaded once.
    harvested: dict[str, Harvest] = {}
    if crawl and (no_limit or (sparse and remaining > 0)):
        known = {normalize(page.url) for page in pages}
        pages = pages + await bfs_crawl(
            base_url,
            fetch_fn,
            known,
            rules.disallow,
            allow=rules.allow,
            limit=remaining,
            delay=delay,
            scope_prefix=scope_prefix,
            harvested=harvested,
        )

    result: list[PageInfo] = []
    seen: set[str] = set()
    for page in pages:
        # Normalized dedupe: /docs and /docs/ are the same page.
        key = normalize(page.url)
        if key in seen or is_disallowed(page.url, rules.disallow, rules.allow):
            continue
        seen.add(key)
        # Merge, not first-wins: keep the sitemap entry's position + lastmod +
        # priority, take the BFS extraction's title + description -- byte-identical
        # to a refetch, minus the fetch.
        crawled = harvested.get(key)
        if crawled is not None and not page.title:
            page.title = crawled.page.title
            page.description = crawled.page.description
            if crawled.requested == page.url:
                # Adopt the crawled URL only when BFS asked for this exact spelling,
                # so it followed the same redirect chain a refetch would. Compare
                # RAW spellings: normalize() erases the trailing slash / index.html
                # that a directory redirect turns on, and that the twin-probe reads
                # -- a normalized compare would keep the pre-redirect URL and probe a
                # twin that can only 404.
                page.url = crawled.page.url
        result.append(page)
        if budget is not None and len(result) >= budget:
            break
    return result

import asyncio
from collections import deque
from dataclasses import dataclass

from extractor import extract
from fetcher import FETCH_CONCURRENCY, as_fetch_result
from models import PageInfo
from urls import in_scope, is_disallowed, normalize, origin_of, same_origin_links

MAX_CRAWL_DEPTH = 2

# Work budget: `limit` bounds pages COLLECTED, but a failed fetch costs a request
# without advancing that counter, so a site that refuses everything would drain the
# whole frontier. Cap attempts at limit * CRAWL_SURPLUS, not just successes.
CRAWL_SURPLUS = 2

# A budget alone is not enough. If a site refuses the first several pages outright,
# it is refusing us -- asking another four hundred times will not change its mind,
# and on the bypass path every ask costs money.
MIN_ATTEMPTS_BEFORE_GIVING_UP = 10
GIVE_UP_FAILURE_RATE = 0.9


@dataclass
class Harvest:
    """A page BFS fetched that the caller already had an entry for.

    `requested` is the URL we asked for; `page.url` is where the server landed it.
    The caller adopts page.url only when `requested` matches its own spelling --
    i.e. this fetch followed the same redirect chain a refetch would have.
    """

    requested: str
    page: PageInfo


async def crawl(
    base_url: str,
    fetch_fn,
    known: set[str],
    disallow: list[str],
    max_depth: int = MAX_CRAWL_DEPTH,
    limit: int | None = None,
    delay: float = 0.0,
    scope_prefix: str | None = None,
    allow: list[str] | tuple = (),
    harvested: dict[str, Harvest] | None = None,
) -> list[PageInfo]:
    """BFS the site, returning the pages the caller did not already know about.

    A page in `known` is still fetched (for its links) but not returned. Pass
    `harvested` to receive those extractions keyed by normalized URL, so the caller
    can merge them instead of re-fetching the same page.
    """
    site_origin = origin_of(base_url)
    start = base_url if base_url.endswith("/") else base_url + "/"
    frontier = deque([(start, 0)])
    visited: set[str] = set()
    results: list[PageInfo] = []
    attempted = 0
    failed = 0
    # No limit means no fetch budget -- but the refusal check below still applies,
    # so a site that says no still stops us.
    budget = None if limit is None else limit * CRAWL_SURPLUS

    while frontier and (limit is None or len(results) < limit):
        if budget is not None and attempted >= budget:
            break
        if (attempted >= MIN_ATTEMPTS_BEFORE_GIVING_UP
                and failed / attempted >= GIVE_UP_FAILURE_RATE):
            # The site is refusing us. Stop asking.
            break
        # One batch (level) at a time -- a page's links can't be fetched before the
        # page. Clamp width to >= 1: a width of 0 would spin this loop with no await.
        batch: list[tuple[str, int]] = []
        width = 1 if delay > 0 else max(1, FETCH_CONCURRENCY)
        if budget is not None:
            width = max(1, min(width, budget - attempted))
        while frontier and len(batch) < width:
            url, depth = frontier.popleft()
            normalized = normalize(url)
            if normalized in visited or is_disallowed(url, disallow, allow):
                continue
            visited.add(normalized)
            batch.append((url, depth))
        if not batch:
            continue

        if delay > 0:
            responses = []
            for url, _ in batch:
                responses.append(await fetch_fn(url))
                await asyncio.sleep(delay)
        else:
            outcomes = await asyncio.gather(
                *(fetch_fn(url) for url, _ in batch), return_exceptions=True
            )
            # A raise from one page must not orphan its nine batch-mates against a
            # client that is about to close -- it just fails its own slot.
            responses = [
                as_fetch_result(url, outcome)
                for (url, _), outcome in zip(batch, outcomes)
            ]

        # Walk the batch in order so `results` stays in discovery order -- the
        # document's section and link order depends on it.
        attempted += len(batch)
        for (url, depth), response in zip(batch, responses):
            if not response.ok:
                failed += 1
                continue
            normalized = normalize(url)
            if normalized in known:
                # We paid for the fetch; hand the extraction back rather than
                # discarding it and letting the caller fetch this page again.
                if harvested is not None:
                    harvested[normalized] = Harvest(
                        requested=url, page=extract(response.text, response.url)
                    )
            elif in_scope(response.url, scope_prefix):
                if limit is None or len(results) < limit:
                    results.append(extract(response.text, response.url))
            if depth < max_depth:
                for link in same_origin_links(response.text, response.url, site_origin):
                    if normalize(link) not in visited and in_scope(link, scope_prefix):
                        frontier.append((link, depth + 1))

    return results

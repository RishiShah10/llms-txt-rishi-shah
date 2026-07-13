import asyncio
import re
from dataclasses import dataclass

import httpx

# Large gzipped sitemaps and heavy doc pages (e.g. MDN) can legitimately take
# longer than 10s; bumping this cuts down on false-positive timeouts.
TIMEOUT_SECONDS = 20.0
HEADERS = {"User-Agent": "llms-txt-generator/1.0 (+https://github.com/RishiShah10)"}

# Status codes worth retrying: rate limiting and transient upstream/server issues.
# Ordinary 4xx (e.g. 404) are not retried since retrying won't change the outcome.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 0.5

# Concurrent page fetches per generation, and the de-facto rate limit (no
# inter-request delay unless robots.txt asks for one). Drop to 5 if 429s spike:
# a 429 escalates to the PAID browser, so over-concurrency is a cost spike.
FETCH_CONCURRENCY = 10


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    ok: bool


def as_fetch_result(url: str, outcome: object) -> FetchResult:
    """Coerce one `gather(..., return_exceptions=True)` slot to a FetchResult.

    Plain gather propagates the first exception and leaves the siblings running
    against a client that's about to close. Collecting instead lets one bad page
    fail only its own slot, so batch order and page count are unchanged.
    """
    if isinstance(outcome, asyncio.CancelledError):
        # Cancellation is control flow, not a fetch failure -- let it propagate.
        raise outcome
    if isinstance(outcome, BaseException):
        return FetchResult(url=url, status=0, text="", ok=False)
    return outcome  # type: ignore[return-value]


async def fetch(url: str, client: httpx.AsyncClient) -> FetchResult:
    last = FetchResult(url=url, status=0, text="", ok=False)
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = await client.get(
                url, follow_redirects=True, timeout=TIMEOUT_SECONDS, headers=HEADERS
            )
            result = FetchResult(
                url=str(response.url),
                status=response.status_code,
                text=response.text,
                ok=response.is_success,
            )
            if response.status_code not in RETRYABLE_STATUS:
                return result
            last = result
        except httpx.HTTPError:
            last = FetchResult(url=url, status=0, text="", ok=False)
        if attempt < MAX_RETRIES:
            # Linear backoff so repeated hits on a struggling upstream don't
            # hammer it harder each time.
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    return last


# Probes are best-effort extras (e.g. .md twin checks): short timeout and no
# retries, since a missed probe only costs a nicer link, never a page.
PROBE_TIMEOUT_SECONDS = 5.0


async def head(url: str, client: httpx.AsyncClient) -> tuple[int, str]:
    try:
        response = await client.head(
            url, follow_redirects=True, timeout=PROBE_TIMEOUT_SECONDS, headers=HEADERS
        )
        return response.status_code, response.headers.get("content-type", "")
    except httpx.HTTPError:
        return 0, ""


# A bot wall is the hardest thing here to detect: it answers 200 with a complete,
# well-formed HTML page (nav, links, prose), so neither the status check nor the
# empty-shell heuristic sees anything wrong. Only the wording gives it away.
# These are the vendors' own interstitial copy. Kept specific on purpose -- a
# generic phrase like "access denied" would fire on real pages that merely
# *discuss* it, and a false positive costs a paid browser render.
_CHALLENGE_MARKERS = (
    # Cloudflare
    "just a moment",
    "verifying connection",
    "attention required",
    "bot or not",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "pardon our interruption",
    # DataDome
    "you have been blocked",
    # PerimeterX / HUMAN
    "please verify you are a human",
)
_TAG_RE = re.compile(r"<[^>]+>")
# Script/style/noscript bodies are not visible prose, and stripping tags alone
# leaves their contents behind. That is what let JS shells through: the data a
# shell hydrates from lives in a <script> blob, so quotes.toscrape.com/js/
# counted 430 "words" where a reader sees 17.
_NOISE_RE = re.compile(r"(?is)<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>")

# Prose is the discriminator: measured, JS shells carry ~17 words, real pages 280+.
# The link bound only spares a legitimately link-heavy index page; it's 12, not 5,
# because a shell with a normal nav bar clears 5 links.
MAX_SHELL_LINKS = 12
MAX_SHELL_WORDS = 50


def _visible_words(html: str) -> int:
    return len(_TAG_RE.sub(" ", _NOISE_RE.sub(" ", html)).split())


def is_challenge(result: FetchResult) -> bool:
    """A bot-protection wall: certain, not a guess.

    Split from the shell heuristic because a wall is certain ("refused us, a browser
    fixes it") while a thin page only *might* be a shell -- a small real page looks
    identical. Only a certain wall is worth warning the user about.
    """
    # A WAF refuses outright with 403/429. Other non-ok statuses (dead host,
    # 404, 5xx) won't be fixed by a browser, so don't waste an escalation.
    if result.status in (403, 429):
        return True
    if not result.ok:
        return False
    return any(marker in result.text.lower()[:2000] for marker in _CHALLENGE_MARKERS)


def is_blocked(result: FetchResult) -> bool:
    if is_challenge(result):
        return True
    if not result.ok:
        return False
    lowered = result.text.lower()
    # The empty-shell heuristic only makes sense for HTML pages. robots.txt
    # (plain text) and sitemap.xml (XML) legitimately have no links or prose, so
    # a non-HTML response must not be treated as blocked -- otherwise it gets
    # escalated to the (paid) browser needlessly, and a browser-rendered sitemap
    # can even break XML parsing.
    head = lowered[:1024]
    if "<html" not in head and "<!doctype html" not in head:
        return False
    # JS shell: content is injected client-side, so the raw HTML is nearly empty
    # of *prose* -- but not of bytes, since the data it hydrates from ships in a
    # <script> blob. Count what a reader would actually see.
    links = lowered.count("<a ")
    words = _visible_words(result.text)
    return links < MAX_SHELL_LINKS and words < MAX_SHELL_WORDS

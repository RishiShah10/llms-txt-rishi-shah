import asyncio
import os
from contextlib import asynccontextmanager

from fetcher import FetchResult

CDP_ENV = "BRIGHT_DATA_CDP_URL"
CONNECT_TIMEOUT_MS = 30000
NAV_TIMEOUT_MS = 45000

# A half-open CDP connection makes close()/stop() HANG (not throw), and this
# teardown runs in a `finally` -- an unbounded hang would wedge the cancelled
# generation. Giving up leaks at worst a remote session Bright Data reaps anyway.
CLOSE_TIMEOUT_SECONDS = 10

# Each browser_fetch opens a real Chromium page on a PAID Bright Data session.
# It must not inherit FETCH_CONCURRENCY -- ten concurrent Chromium pages is both
# slow and expensive.
BROWSER_CONCURRENCY = 2


async def _connect():
    # Lazy import so playwright is only touched when a real endpoint is set,
    # keeping tests and the default (direct-fetch) path dependency-free.
    cdp_url = os.getenv(CDP_ENV)
    if not cdp_url:
        return None, None
    pw = None
    try:
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(cdp_url, timeout=CONNECT_TIMEOUT_MS)
        return browser, pw
    except Exception:
        # A started-but-unconnected Playwright still owns a Node subprocess;
        # without stopping it, a bad CDP URL leaks one per failed request.
        if pw is not None:
            try:
                await asyncio.wait_for(pw.stop(), timeout=CLOSE_TIMEOUT_SECONDS)
            except Exception:
                pass
        return None, None


async def _cleanup(browser, pw):
    for closer in (getattr(browser, "close", None), getattr(pw, "stop", None)):
        if closer:
            try:
                # TimeoutError is an Exception, so the existing handler covers it.
                await asyncio.wait_for(closer(), timeout=CLOSE_TIMEOUT_SECONDS)
            except Exception:
                pass


@asynccontextmanager
async def browser_session():
    browser, pw = await _connect()
    try:
        yield browser
    finally:
        await _cleanup(browser, pw)


async def browser_fetch(browser, url: str, warnings: list[str]) -> FetchResult:
    if browser is None:
        return FetchResult(url=url, status=0, text="", ok=False)
    try:
        page = await browser.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded",
                                       timeout=NAV_TIMEOUT_MS)
            status = response.status if response else 200
            content = await page.content()
            return FetchResult(url=page.url, status=status, text=content, ok=status < 400)
        finally:
            await page.close()
    except Exception as error:
        warnings.append(
            f"browser fetch failed for {url}: {(str(error).splitlines() or [''])[0]}"
        )
        return FetchResult(url=url, status=0, text="", ok=False)

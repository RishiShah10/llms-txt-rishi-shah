import httpx
import pytest

import fetcher
from fetcher import (
    MAX_SHELL_WORDS,
    _TAG_RE,
    FetchResult,
    fetch,
    is_blocked,
)

pytestmark = pytest.mark.anyio


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_returns_text_on_200():
    async with _client(lambda req: httpx.Response(200, text="<html>ok</html>")) as client:
        result = await fetch("https://a.com/", client)
    assert result.ok and result.status == 200 and "ok" in result.text


async def test_fetch_marks_non_200_not_ok():
    async with _client(lambda req: httpx.Response(404, text="nope")) as client:
        result = await fetch("https://a.com/missing", client)
    assert not result.ok and result.status == 404


async def test_fetch_handles_transport_error(monkeypatch):
    monkeypatch.setattr(fetcher, "RETRY_BACKOFF_SECONDS", 0)

    def handler(req):
        raise httpx.ConnectError("boom")

    async with _client(handler) as client:
        result = await fetch("https://a.com/", client)
    assert not result.ok and result.status == 0


async def test_fetch_success_on_first_try_does_not_retry(monkeypatch):
    monkeypatch.setattr(fetcher, "RETRY_BACKOFF_SECONDS", 0)
    calls = 0

    def handler(req):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="ok")

    async with _client(handler) as client:
        result = await fetch("https://a.com/", client)
    assert result.ok and calls == 1


async def test_fetch_retries_after_503_then_succeeds(monkeypatch):
    monkeypatch.setattr(fetcher, "RETRY_BACKOFF_SECONDS", 0)
    calls = 0

    def handler(req):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, text="ok")

    async with _client(handler) as client:
        result = await fetch("https://a.com/", client)
    assert result.ok and result.status == 200 and calls == 2


async def test_fetch_gives_up_after_persistent_503(monkeypatch):
    monkeypatch.setattr(fetcher, "RETRY_BACKOFF_SECONDS", 0)
    calls = 0

    def handler(req):
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="unavailable")

    async with _client(handler) as client:
        result = await fetch("https://a.com/", client)
    assert not result.ok and result.status == 503
    assert calls == fetcher.MAX_RETRIES + 1


async def test_fetch_does_not_retry_404(monkeypatch):
    monkeypatch.setattr(fetcher, "RETRY_BACKOFF_SECONDS", 0)
    calls = 0

    def handler(req):
        nonlocal calls
        calls += 1
        return httpx.Response(404, text="nope")

    async with _client(handler) as client:
        result = await fetch("https://a.com/missing", client)
    assert not result.ok and calls == 1


async def test_fetch_retries_transport_error_and_gives_up(monkeypatch):
    monkeypatch.setattr(fetcher, "RETRY_BACKOFF_SECONDS", 0)
    calls = 0

    def handler(req):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("boom")

    async with _client(handler) as client:
        result = await fetch("https://a.com/", client)
    assert not result.ok and result.status == 0
    assert calls == fetcher.MAX_RETRIES + 1


def _r(status, text="", ok=None):
    ok = (200 <= status < 300) if ok is None else ok
    return FetchResult(url="https://a.com", status=status, text=text, ok=ok)


def test_is_blocked_on_403_and_429():
    assert is_blocked(_r(403))
    assert is_blocked(_r(429))


def test_is_blocked_on_challenge_title():
    assert is_blocked(_r(200, "<title>Just a moment...</title>"))


def test_is_blocked_on_js_shell():
    assert is_blocked(_r(200, "<html><body><div id='root'></div></body></html>"))


def test_not_blocked_on_rich_page():
    html = "<html><body>" + "".join(
        f"<a href='/{i}'>link {i} some words here</a>" for i in range(10)
    ) + "</body></html>"
    assert not is_blocked(_r(200, html))


def test_not_blocked_on_non_html():
    # robots.txt (plain text) and sitemap.xml (XML) have no links/prose but must
    # not be treated as blocked -- the empty-shell heuristic is HTML-only.
    assert not is_blocked(_r(200, "User-agent: *\nDisallow: /admin/\n"))
    assert not is_blocked(
        _r(200, '<?xml version="1.0"?><urlset><url><loc>https://a.com/x</loc></url></urlset>')
    )


def test_not_blocked_on_404_or_transport_error():
    assert not is_blocked(_r(404, "", ok=False))
    assert not is_blocked(_r(0, "", ok=False))


def _shell(nav_links: int, script_words: int) -> str:
    # A real JS shell: a nav bar, an empty mount point, and the page's data
    # sitting in a <script> blob that a reader never sees.
    nav = "".join(f'<a href="/{i}">Link {i}</a>' for i in range(nav_links))
    blob = " ".join(f'"quote{i}": "text"' for i in range(script_words))
    return (
        f"<!DOCTYPE html><html><body><nav>{nav}</nav><div id='root'></div>"
        f"<script>var data = {{{blob}}};</script></body></html>"
    )


def test_js_shell_is_blocked_even_though_its_script_blob_is_huge():
    # The bug this pins: stripping tags leaves <script> CONTENTS behind, so the
    # data a shell hydrates from counted as visible prose. quotes.toscrape.com/js/
    # measured 430 "words" where a reader sees 17, and sailed past the threshold.
    page = _shell(nav_links=6, script_words=200)
    assert len(_TAG_RE.sub(" ", page).split()) > MAX_SHELL_WORDS  # the old count
    assert is_blocked(FetchResult(url="https://a.com/js/", status=200, text=page, ok=True))


def test_nav_bar_alone_does_not_hide_a_shell():
    # The old bound was `links < 5` -- below a typical nav bar, so any shell with
    # chrome escaped. quotes.toscrape.com/js/ has 6 links.
    page = _shell(nav_links=6, script_words=200)
    assert page.lower().count("<a ") == 6
    assert is_blocked(FetchResult(url="https://a.com/js/", status=200, text=page, ok=True))


def test_a_real_content_page_is_not_escalated():
    # A false positive costs a PAID browser render, so this is the guard rail.
    # Real pages measured 280+ words of prose; shells 17.
    prose = " ".join("word" for _ in range(300))
    nav = "".join(f'<a href="/{i}">L{i}</a>' for i in range(8))
    page = f"<!DOCTYPE html><html><body><nav>{nav}</nav><p>{prose}</p></body></html>"
    assert not is_blocked(FetchResult(url="https://a.com/", status=200, text=page, ok=True))


def test_a_link_heavy_index_page_is_not_escalated():
    # Many links, little prose -- a legitimate link list, not a shell. This is
    # the only thing the link bound is protecting.
    links = "".join(f'<a href="/{i}">L{i}</a>' for i in range(40))
    page = f"<!DOCTYPE html><html><body><ul>{links}</ul></body></html>"
    assert not is_blocked(FetchResult(url="https://a.com/index", status=200, text=page, ok=True))


# The real Imperva/Incapsula interstitial pokemon.com serves to AWS datacenter
# IPs: HTTP 200, a complete well-formed page with nav and prose. Neither the
# status check nor the empty-shell heuristic sees anything wrong with it.
_IMPERVA_WALL = """<!DOCTYPE html><html><head><title>Pardon Our Interruption</title></head>
<body><h1>Pardon Our Interruption</h1>
<p>As you were browsing something about your browser made us think you were a bot.
There are a few reasons this might happen:</p>
<ul><li>You're a power user moving through this website with super-human speed.</li>
<li>You've disabled JavaScript in your web browser.</li>
<li>A third-party browser plugin is preventing JavaScript from running.</li></ul>
<p>To regain access, please make sure that JavaScript and cookies are enabled.</p>
""" + "".join(f'<a href="/{i}">Link {i}</a>' for i in range(60)) + "</body></html>"


def test_imperva_bot_wall_is_detected():
    # The bug: this is a 200 with 500+ words and 60 links, so the shell heuristic
    # passes it as a real page. Only the wording gives it away, and "pardon our
    # interruption" was not in the marker list. 25 of these shipped as a
    # "spec-compliant" llms.txt for pokemon.com.
    result = FetchResult(url="https://a.com/x", status=200, text=_IMPERVA_WALL, ok=True)
    assert not (_TAG_RE.sub(" ", _IMPERVA_WALL).split().__len__() < MAX_SHELL_WORDS), \
        "precondition: the wall has plenty of words, so only the marker can catch it"
    assert is_blocked(result)


def test_a_page_merely_mentioning_blocking_is_not_flagged():
    # A false positive costs a PAID browser render. The markers are the vendors'
    # own interstitial copy for exactly this reason -- a generic phrase like
    # "access denied" would fire on real pages that only discuss it.
    prose = "This guide explains how to handle access denied errors and rate limits. " * 20
    page = f"<!DOCTYPE html><html><head><title>Handling 403s</title></head><body><p>{prose}</p></body></html>"
    assert not is_blocked(FetchResult(url="https://a.com/", status=200, text=page, ok=True))

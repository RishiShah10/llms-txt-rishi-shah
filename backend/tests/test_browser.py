import pytest

from browser import _connect, browser_fetch, browser_session

pytestmark = pytest.mark.anyio


class _FakeResponse:
    def __init__(self, status): self.status = status


class _FakePage:
    def __init__(self, html, status=200, raise_on_goto=False):
        self._html, self._status, self._raise = html, status, raise_on_goto
        self.url = "https://a.com/rendered"

    async def goto(self, url, **kwargs):
        if self._raise:
            raise RuntimeError("nav failed")
        self.url = url
        return _FakeResponse(self._status)

    async def content(self): return self._html
    async def close(self): pass


class _FakeBrowser:
    def __init__(self, page): self._page = page
    async def new_page(self): return self._page


async def test_browser_fetch_returns_rendered_html_without_warning():
    # Falling back to the browser is normal operation, not a problem. It is
    # reported on the progress event (Event.browser), where the live log can show
    # it -- putting it in `warnings` made every rendered page look like a fault.
    # A FAILED render is still a warning; see the test below.
    warnings = []
    result = await browser_fetch(
        _FakeBrowser(_FakePage("<html>rendered</html>")), "https://a.com", warnings
    )
    assert result.ok and "rendered" in result.text
    assert warnings == []


async def test_browser_fetch_falls_back_on_error():
    warnings = []
    result = await browser_fetch(
        _FakeBrowser(_FakePage("", raise_on_goto=True)), "https://a.com", warnings
    )
    assert not result.ok
    assert any("browser fetch failed" in w for w in warnings)


async def test_browser_fetch_none_browser_is_not_ok():
    assert not (await browser_fetch(None, "https://a.com", [])).ok


async def test_browser_session_without_creds_yields_none(monkeypatch):
    monkeypatch.delenv("BRIGHT_DATA_CDP_URL", raising=False)
    async with browser_session() as b:
        assert b is None


async def test_browser_fetch_falls_back_on_error_with_empty_message():
    # A raised exception with no message makes str(error) == "", so
    # .splitlines() returns [] -- indexing [0] must not raise IndexError
    # out of the very except handler meant to produce a graceful fallback.
    warnings = []

    class _RaisingBrowser:
        async def new_page(self):
            raise Exception()

    result = await browser_fetch(_RaisingBrowser(), "https://a.com", warnings)
    assert not result.ok


async def test_connect_stops_playwright_on_partial_connect_failure(monkeypatch):
    monkeypatch.setenv("BRIGHT_DATA_CDP_URL", "wss://example.com/cdp")

    class _FakeChromium:
        async def connect_over_cdp(self, cdp_url, timeout):
            raise RuntimeError("connect failed")

    class _FakePw:
        def __init__(self):
            self.stopped = False
            self.chromium = _FakeChromium()

        async def stop(self):
            self.stopped = True

    fake_pw = _FakePw()

    class _FakeAsyncPlaywright:
        async def start(self):
            return fake_pw

    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: _FakeAsyncPlaywright()
    )

    browser, pw = await _connect()

    assert browser is None
    assert pw is None
    assert fake_pw.stopped is True

import asyncio

import httpx
from fastapi.testclient import TestClient

import app as app_mod

_SITEMAP = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://a.com/one</loc></url>
</urlset>"""

_PAGE = "<html><head><title>T</title></head><body><p>Hi</p></body></html>"

_PARAMS = {"url": "https://a.com", "max_pages": 10, "crawl": False}


def _ok_client():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=_SITEMAP)
        if request.method == "HEAD":
            return httpx.Response(404)
        return httpx.Response(200, text=_PAGE)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _dead_client():
    # Every page fails -> zero pages -> _run_generation raises HTTPException(422).
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))


def _drain(websocket):
    """Collect every frame until the terminal one."""
    frames = []
    while True:
        frame = websocket.receive_json()
        frames.append(frame)
        if frame["type"] in ("done", "error"):
            return frames


def test_streams_events_then_exactly_one_done(monkeypatch):
    monkeypatch.setattr(app_mod, "make_client", _ok_client)
    with TestClient(app_mod.app) as client:
        with client.websocket_connect("/ws/generate") as websocket:
            websocket.send_json(_PARAMS)
            frames = _drain(websocket)

    assert frames[-1]["type"] == "done"
    assert [f["type"] for f in frames].count("done") == 1
    assert any(f["type"] == "event" for f in frames[:-1])
    assert frames[-1]["result"]["llms_txt"].startswith("# ")
    assert "public_url" in frames[-1]["result"]


def test_zero_page_site_yields_an_error_frame_not_a_dead_socket(monkeypatch):
    # HTTPException raised inside an ACCEPTED websocket does NOT become an error
    # frame -- Starlette emits http.response.start and uvicorn kills the socket.
    # The handler must catch it. This is the most common failure there is.
    monkeypatch.setattr(app_mod, "make_client", _dead_client)
    with TestClient(app_mod.app) as client:
        with client.websocket_connect("/ws/generate") as websocket:
            websocket.send_json(_PARAMS)
            frames = _drain(websocket)

    assert frames[-1]["type"] == "error"
    assert "Could not generate" in frames[-1]["detail"]


def test_absent_origin_is_accepted(monkeypatch):
    # TestClient sends no Origin header, and neither does wscat. CORS only ever
    # gates browsers, so an absent Origin must not be rejected.
    monkeypatch.setattr(app_mod, "make_client", _ok_client)
    monkeypatch.setenv("FRONTEND_ORIGIN", "https://real.example.com")
    with TestClient(app_mod.app) as client:
        with client.websocket_connect("/ws/generate") as websocket:
            websocket.send_json(_PARAMS)
            frames = _drain(websocket)
    assert frames[-1]["type"] == "done"


def test_ws_result_matches_the_post_endpoint(monkeypatch):
    monkeypatch.setattr(app_mod, "make_client", _ok_client)
    with TestClient(app_mod.app) as client:
        posted = client.post("/generate", json=_PARAMS).json()
        with client.websocket_connect("/ws/generate") as websocket:
            websocket.send_json(_PARAMS)
            streamed = _drain(websocket)[-1]["result"]

    assert streamed["llms_txt"] == posted["llms_txt"]
    assert streamed["public_url"] == posted["public_url"]


class _CountingSemaphore(asyncio.Semaphore):
    def __init__(self, value: int):
        super().__init__(value)
        self.acquires = 0

    async def acquire(self) -> bool:
        self.acquires += 1
        return await super().acquire()


def test_ws_acquires_the_generation_slot_exactly_once(monkeypatch):
    # The cap now lives in _run_generation so it covers POST /generate too. If
    # ws_generate ALSO wrapped itself in `async with _generation_slots`, the two
    # acquisitions would deadlock every WS request once the cap is reached (and
    # with a cap of 1, immediately). Counting the acquisitions catches that
    # regression here, deterministically, instead of hanging the suite.
    slots = _CountingSemaphore(app_mod.MAX_CONCURRENT_GENERATIONS)
    monkeypatch.setattr(app_mod, "_generation_slots", slots)
    monkeypatch.setattr(app_mod, "make_client", _ok_client)

    with TestClient(app_mod.app) as client:
        with client.websocket_connect("/ws/generate") as websocket:
            websocket.send_json(_PARAMS)
            frames = _drain(websocket)

    assert frames[-1]["type"] == "done"
    assert slots.acquires == 1, "the WS path acquired the slot twice -- that deadlocks"
    assert slots._value == app_mod.MAX_CONCURRENT_GENERATIONS, "the slot was never released"


def test_the_post_paths_are_under_the_same_cap(monkeypatch):
    # The cap used to guard ONLY the WebSocket, so POST /generate and
    # /check-changes could fan out unbounded generations -- each bypass=True one
    # opening its own PAID Bright Data session.
    slots = _CountingSemaphore(app_mod.MAX_CONCURRENT_GENERATIONS)
    monkeypatch.setattr(app_mod, "_generation_slots", slots)
    monkeypatch.setattr(app_mod, "make_client", _ok_client)

    with TestClient(app_mod.app) as client:
        assert client.post("/generate", json=_PARAMS).status_code == 200
        assert slots.acquires == 1

        assert client.post("/check-changes", json=_PARAMS).status_code == 200
        assert slots.acquires == 2

    assert slots._value == app_mod.MAX_CONCURRENT_GENERATIONS


def test_handler_accepts_before_closing_on_a_bad_origin():
    # THE CLOSE-CODE TRAP. A close() BEFORE accept() makes uvicorn discard the
    # code and send a bare HTTP 403 -- the browser never sees 1008. TestClient
    # fabricates the code off the raw ASGI message, so asserting the code alone
    # would pass green against a broken production path.
    #
    # So assert the ORDERING instead: accept() must be called before close().
    #
    # Comment lines must be stripped first -- the handler's own leading comment
    # contains the literal substrings "accept()" and "close(" (explaining this
    # very trap), so searching the raw source finds those words in the comment
    # and never reaches the real statements, making the assertion vacuous.
    import inspect
    import re

    source = inspect.getsource(app_mod.ws_generate)
    code_lines = [
        line for line in source.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    code = "\n".join(code_lines)

    accept_match = re.search(r"await websocket\.accept\(\)", code)
    close_match = re.search(r"await websocket\.close\(", code)
    assert accept_match and close_match, "expected both accept() and close( statements in source"
    assert accept_match.start() < close_match.start(), (
        "close() must never precede accept() -- uvicorn drops the code"
    )

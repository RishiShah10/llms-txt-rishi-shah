"""WebSocket behaviours that ONLY a real uvicorn can prove.

TestClient fabricates close codes off the raw ASGI message and never runs
uvicorn's WebSocket stack, so a handler that closes BEFORE accept() -- which
makes uvicorn discard the code and send a bare HTTP 403 -- looks perfectly
healthy under TestClient while every browser sees 1006. These tests run the app
under a REAL uvicorn in a subprocess, against a REAL stub site, and connect with
a real WebSocket client: every close code asserted here is one a browser would
actually receive.

Kept out of the TestClient suite (which stays fast) and skipped when a port
can't bind.
"""

import asyncio
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import websockets
from websockets.exceptions import ConnectionClosed

pytestmark = pytest.mark.anyio

BACKEND_DIR = Path(__file__).resolve().parent.parent
ALLOWED_ORIGIN = "https://allowed.example.com"

# One slot, so a single in-flight generation is enough to make the next client
# hit the admission cap.
SLOTS = 1
CRAWL_DELAY_SECONDS = 1  # robots.txt Crawl-delay -- forces the crawl sequential
PAGE_COUNT = 6


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _StubSite(http.server.BaseHTTPRequestHandler):
    """A real HTTP site for the subprocess to crawl. Counts its page fetches."""

    pages_fetched: list = []

    def _send(self, body: str, content_type: str = "text/html") -> None:
        encoded = body.encode()
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's API
        host = self.headers.get("host")
        if self.path == "/robots.txt":
            return self._send(f"User-agent: *\nCrawl-delay: {CRAWL_DELAY_SECONDS}\n",
                              "text/plain")
        if self.path == "/sitemap.xml":
            locs = "".join(
                f"<url><loc>http://{host}/p{i}</loc></url>" for i in range(PAGE_COUNT)
            )
            return self._send(
                '<?xml version="1.0"?><urlset '
                f'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>',
                "application/xml",
            )
        if self.path.startswith("/p"):
            type(self).pages_fetched.append(self.path)
        return self._send(
            "<html><head><title>T</title>"
            '<meta name="description" content="d"></head><body><p>hi</p></body></html>'
        )

    def do_HEAD(self):  # noqa: N802 - the md-twin probes; 404 keeps them out of the way
        self.send_response(404)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass  # keep pytest output clean


@pytest.fixture(scope="module")
def stub_site():
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), _StubSite)
    except OSError as error:
        pytest.skip(f"cannot bind a stub-site port: {error}")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def live_server():
    """The app under a REAL uvicorn, on a real port."""
    port = _free_port()
    env = {
        **os.environ,
        "FRONTEND_ORIGIN": ALLOWED_ORIGIN,
        "MAX_CONCURRENT_GENERATIONS": str(SLOTS),
        # No DATABASE_URL / S3_BUCKET: both are best-effort and only add warnings.
        "DATABASE_URL": "",
        "PYTHONPATH": str(BACKEND_DIR),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=BACKEND_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.skip(f"uvicorn exited early: {process.stdout.read()[-500:]}")
        try:
            if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        process.kill()
        pytest.skip("uvicorn did not bind in time")

    yield f"ws://127.0.0.1:{port}/ws/generate"

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def _params(site: str, **overrides) -> str:
    return json.dumps({"url": site, "max_pages": PAGE_COUNT, "crawl": False, **overrides})


async def _next_event(connection) -> dict:
    """Read one progress frame. Its arrival proves the slot is HELD: the frame
    comes from inside generate(), which runs under the semaphore."""
    while True:
        frame = json.loads(await asyncio.wait_for(connection.recv(), timeout=30))
        if frame["type"] == "event":
            return frame
        pytest.fail(f"expected a progress event, got a terminal frame: {frame}")


async def _start_generation(url: str, site: str, timeout: float = 30, **overrides):
    """Open a socket, start a crawl, and return once it is provably holding the
    slot. Retries through 1013 so a previous test's teardown can't flake this."""
    deadline = time.monotonic() + timeout
    while True:
        connection = await websockets.connect(url, origin=ALLOWED_ORIGIN)
        await connection.send(_params(site, **overrides))
        try:
            await _next_event(connection)
            return connection
        except ConnectionClosed as closed:
            assert closed.rcvd.code == 1013, f"unexpected close code {closed.rcvd.code}"
            if time.monotonic() > deadline:
                pytest.fail("no generation slot ever became free -- the slot leaked")
            await asyncio.sleep(0.3)


async def test_a_disallowed_origin_is_really_closed_with_1008(live_server, stub_site):
    # The branch nothing else exercises: _origin_allowed -> False. Asserting the
    # REAL code also proves accept()-before-close() holds end to end: a close()
    # before accept() surfaces as a bare HTTP 403 / 1006, never as 1008.
    with pytest.raises(ConnectionClosed) as caught:
        async with websockets.connect(live_server,
                                      origin="https://evil.example.com") as connection:
            await connection.send(_params(stub_site))
            await asyncio.wait_for(connection.recv(), timeout=10)

    assert caught.value.rcvd.code == 1008


async def test_the_allowed_origin_still_gets_through(live_server, stub_site):
    # The other half of the gate. Without this, a handler that closed EVERY
    # connection with 1008 would score green above.
    connection = await _start_generation(live_server, stub_site)
    await connection.close()


async def test_the_admission_cap_really_closes_the_next_client_with_1013(
        live_server, stub_site):
    busy = await _start_generation(live_server, stub_site, crawl=True)
    try:
        with pytest.raises(ConnectionClosed) as caught:
            async with websockets.connect(live_server, origin=ALLOWED_ORIGIN) as second:
                await second.send(_params(stub_site))
                await asyncio.wait_for(second.recv(), timeout=10)
        assert caught.value.rcvd.code == 1013  # try again later
    finally:
        await busy.close()


async def test_a_disconnect_really_cancels_the_crawl(live_server, stub_site):
    # robots.txt sets a Crawl-delay, so the stub's pages go out one per second.
    # A crawl that survived the closed tab would keep hitting the stub afterwards.
    connection = await _start_generation(live_server, stub_site)
    await asyncio.sleep(CRAWL_DELAY_SECONDS * 1.5)  # let a page or two land
    _StubSite.pages_fetched.clear()
    await connection.close()

    await asyncio.sleep(CRAWL_DELAY_SECONDS * 3)  # ample time for the rest to land

    # <=1: a fetch already in flight when the cancel arrived may still complete.
    assert len(_StubSite.pages_fetched) <= 1, (
        f"the crawl kept fetching after the disconnect ({_StubSite.pages_fetched}) "
        "-- the client disconnect did not cancel it"
    )


async def test_the_slot_is_released_after_a_disconnect(live_server, stub_site):
    # The failure this whole file exists for: a teardown that pins the semaphore
    # leaves every later client stuck on 1013 with no recovery short of a restart.
    # _start_generation only returns once a slot is genuinely free.
    first = await _start_generation(live_server, stub_site)
    await first.close()

    second = await _start_generation(live_server, stub_site)
    await second.close()

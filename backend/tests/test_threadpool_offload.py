import httpx
import pytest
from fastapi.testclient import TestClient

import app as app_mod

_HOME = "<html><head><title>A</title></head><body><p>Hi</p></body></html>"


def _mock_client():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.method == "HEAD":
            return httpx.Response(404)
        if request.url.path.endswith(".xml"):
            return httpx.Response(404)
        return httpx.Response(200, text=_HOME)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_blocking_db_and_s3_calls_are_offloaded(monkeypatch):
    # Endpoints now run on the event loop, so an un-offloaded psycopg or boto3
    # round trip stalls every other coroutine on the task. Assert each one is
    # routed through run_in_threadpool rather than called inline.
    offloaded: list[str] = []
    real = app_mod.run_in_threadpool

    async def spy(func, *args, **kwargs):
        offloaded.append(getattr(func, "__name__", repr(func)))
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(app_mod, "run_in_threadpool", spy)
    monkeypatch.setattr(app_mod, "make_client", _mock_client)

    with TestClient(app_mod.app) as client:
        response = client.post(
            "/generate",
            json={"url": "https://a.com", "crawl": False, "auto_update": True},
        )

    assert response.status_code == 200
    assert "_store_file" in offloaded
    assert "_persist" in offloaded
    assert "enroll_auto_update" in offloaded

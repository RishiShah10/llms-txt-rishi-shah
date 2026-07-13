import httpx
from fastapi.testclient import TestClient

import app as app_module
import storage
from app import app

MOCK = {
    "https://a.com/robots.txt": "Sitemap: https://a.com/sitemap.xml",
    "https://a.com/sitemap.xml": '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://a.com/docs</loc></url></urlset>',
    "https://a.com/": "<html><head><title>Acme</title></head></html>",
    "https://a.com/docs": "<html><head><title>Docs</title></head></html>",
}


def _handler(request):
    url = str(request.url)
    return httpx.Response(200, text=MOCK[url]) if url in MOCK else httpx.Response(404, text="")


def test_health():
    assert TestClient(app).get("/health").json() == {"status": "ok"}


def test_generate_endpoint(monkeypatch):
    monkeypatch.setattr(
        app_module, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
    )
    response = TestClient(app).post("/generate", json={"url": "https://a.com"})
    assert response.status_code == 200
    body = response.json()
    assert body["llms_txt"].startswith("# Acme")
    assert body["public_url"] == "https://bucket.s3.us-east-2.amazonaws.com/a.com.txt"


def test_generate_endpoint_survives_storage_failure(monkeypatch):
    # A storage failure (e.g. S3 outage or missing bucket) must degrade
    # gracefully -- the generation still succeeds, just without a public_url.
    monkeypatch.setattr(
        app_module, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
    )

    def _boom(content, key):
        raise RuntimeError("bucket does not exist")

    monkeypatch.setattr(storage, "upload_llms_txt", _boom)
    response = TestClient(app).post("/generate", json={"url": "https://a.com"})
    assert response.status_code == 200
    body = response.json()
    assert body["public_url"] is None
    assert any("file storage skipped" in w for w in body["warnings"])


def test_generate_endpoint_survives_storage_failure_with_empty_message(monkeypatch):
    # An exception whose str() is empty makes splitlines() return [], so indexing
    # [0] raised IndexError -- turning a best-effort storage skip into a 500.
    monkeypatch.setattr(
        app_module, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
    )

    def _boom(content, key):
        raise RuntimeError()  # str() is ""

    monkeypatch.setattr(storage, "upload_llms_txt", _boom)
    response = TestClient(app).post("/generate", json={"url": "https://a.com"})
    assert response.status_code == 200
    body = response.json()
    assert body["public_url"] is None
    assert any("file storage skipped" in w for w in body["warnings"])


def test_generate_endpoint_returns_422_when_no_pages_discovered(monkeypatch):
    # robots.txt, sitemap.xml, and the homepage all 404 — nothing to crawl or
    # extract, so the generator yields zero pages. This must surface as a
    # loud failure, not a fake 200 with an empty file.
    def _all_404_handler(request):
        return httpx.Response(404, text="")

    monkeypatch.setattr(
        app_module, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_all_404_handler)),
    )
    response = TestClient(app).post("/generate", json={"url": "https://unreachable.example"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str) and detail.strip()
    assert "unreachable.example" in detail or "homepage" in detail or "no pages" in detail


def test_generate_endpoint_accepts_custom_max_pages(monkeypatch):
    monkeypatch.setattr(
        app_module, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
    )
    response = TestClient(app).post("/generate", json={"url": "https://a.com", "max_pages": 5})
    assert response.status_code == 200
    assert response.json()["llms_txt"].startswith("# ")


def test_generate_endpoint_accepts_null_max_pages_as_no_limit(monkeypatch):
    monkeypatch.setattr(
        app_module, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
    )
    response = TestClient(app).post("/generate", json={"url": "https://a.com", "max_pages": None})
    assert response.status_code == 200
    assert response.json()["llms_txt"].startswith("# ")


MULTI_PAGE_SITE = {
    "https://b.com/robots.txt": "Sitemap: https://b.com/sitemap.xml",
    "https://b.com/sitemap.xml": (
        '<?xml version="1.0"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://b.com/p1</loc></url>"
        "<url><loc>https://b.com/p2</loc></url>"
        "<url><loc>https://b.com/p3</loc></url>"
        "<url><loc>https://b.com/p4</loc></url>"
        "<url><loc>https://b.com/p5</loc></url>"
        "</urlset>"
    ),
    "https://b.com/": "<html><head><title>Bravo Home</title></head></html>",
    "https://b.com/p1": "<html><head><title>Page 1</title></head></html>",
    "https://b.com/p2": "<html><head><title>Page 2</title></head></html>",
    "https://b.com/p3": "<html><head><title>Page 3</title></head></html>",
    "https://b.com/p4": "<html><head><title>Page 4</title></head></html>",
    "https://b.com/p5": "<html><head><title>Page 5</title></head></html>",
}


def _multi_page_handler(request):
    url = str(request.url)
    return httpx.Response(200, text=MULTI_PAGE_SITE[url]) if url in MULTI_PAGE_SITE else httpx.Response(404, text="")


def test_generate_endpoint_max_pages_caps_discovered_pages(monkeypatch):
    monkeypatch.setattr(
        app_module, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_multi_page_handler)),
    )
    response = TestClient(app).post("/generate", json={"url": "https://b.com", "max_pages": 2})
    assert response.status_code == 200
    assert len(response.json()["pages"]) == 3


def test_generate_endpoint_zero_max_pages_returns_all_discovered_pages(monkeypatch):
    monkeypatch.setattr(
        app_module, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_multi_page_handler)),
    )
    response = TestClient(app).post("/generate", json={"url": "https://b.com", "max_pages": 0})
    assert response.status_code == 200
    body = response.json()
    assert len(body["pages"]) == 6
    assert "no page limit set — fetched all discovered pages" in body["warnings"]


def test_generate_endpoint_default_max_pages_returns_pages(monkeypatch):
    monkeypatch.setattr(
        app_module, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_multi_page_handler)),
    )
    response = TestClient(app).post("/generate", json={"url": "https://b.com"})
    assert response.status_code == 200
    assert len(response.json()["pages"]) > 0


# Sparse sitemap (1 page) so the BFS top-up would trigger by default; the
# homepage links to a page absent from the sitemap so `crawl` toggles whether
# it shows up in the result.
SPARSE_SITE_WITH_EXTRA_LINK = {
    "https://c.com/robots.txt": "Sitemap: https://c.com/sitemap.xml",
    "https://c.com/sitemap.xml": (
        '<?xml version="1.0"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://c.com/known</loc></url>"
        "</urlset>"
    ),
    "https://c.com/": '<html><head><title>Charlie</title></head><body><a href="/extra">extra</a></body></html>',
    "https://c.com/known": "<html><head><title>Known</title></head></html>",
    "https://c.com/extra": "<html><head><title>Extra</title></head></html>",
}


def _sparse_handler(request):
    url = str(request.url)
    return httpx.Response(200, text=SPARSE_SITE_WITH_EXTRA_LINK[url]) if url in SPARSE_SITE_WITH_EXTRA_LINK else httpx.Response(404, text="")


def test_generate_endpoint_crawl_false_skips_bfs_topup(monkeypatch):
    monkeypatch.setattr(
        app_module, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_sparse_handler)),
    )
    response = TestClient(app).post("/generate", json={"url": "https://c.com", "crawl": False})
    assert response.status_code == 200
    urls = [page["url"] for page in response.json()["pages"]]
    assert "https://c.com/extra" not in urls


def test_generate_endpoint_crawl_true_runs_bfs_topup(monkeypatch):
    monkeypatch.setattr(
        app_module, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_sparse_handler)),
    )
    response = TestClient(app).post("/generate", json={"url": "https://c.com", "crawl": True})
    assert response.status_code == 200
    urls = [page["url"] for page in response.json()["pages"]]
    assert "https://c.com/extra" in urls


def test_generate_endpoint_accepts_bypass(monkeypatch):
    # bypass=True with no BRIGHT_DATA_CDP_URL → gracefully falls back to direct,
    # so a normal mocked site still returns 200.
    monkeypatch.delenv("BRIGHT_DATA_CDP_URL", raising=False)
    monkeypatch.setattr(
        app_module, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
    )
    # `dict.setdefault(...) or real_generate(...)` would short-circuit here since
    # setdefault returns the (truthy, non-empty) args tuple — so the spy must be a
    # real function that unconditionally calls through after capturing args.
    captured = {}
    real_generate = app_module.generate

    def spy(*a, **k):
        captured["args"] = a
        return real_generate(*a, **k)

    monkeypatch.setattr(app_module, "generate", spy)
    response = TestClient(app).post("/generate", json={"url": "https://a.com", "bypass": True})
    assert response.status_code == 200
    assert captured["args"][-1] is True   # bypass passed positionally as the last arg


BLOCKED_SITE = {
    "https://d.com/robots.txt": "User-agent: *\nDisallow: /\nSitemap: https://d.com/sitemap.xml",
    "https://d.com/sitemap.xml": (
        '<?xml version="1.0"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://d.com/docs</loc></url>"
        "</urlset>"
    ),
    "https://d.com/": "<html><head><title>Delta</title></head></html>",
    "https://d.com/docs": "<html><head><title>Docs</title></head></html>",
}


def _blocked_handler(request):
    url = str(request.url)
    return httpx.Response(200, text=BLOCKED_SITE[url]) if url in BLOCKED_SITE else httpx.Response(404, text="")


def test_generate_endpoint_honor_robots_toggle(monkeypatch):
    monkeypatch.setattr(
        app_module, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_blocked_handler)),
    )
    client = TestClient(app)
    # Default: robots.txt fully disallows the site → nothing to generate.
    assert client.post("/generate", json={"url": "https://d.com"}).status_code == 422
    # Explicit opt-out: restrictions ignored, generation succeeds.
    response = client.post("/generate", json={"url": "https://d.com", "honor_robots": False})
    assert response.status_code == 200
    assert response.json()["llms_txt"].startswith("# Delta")


def test_generate_endpoint_accepts_enhance(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    curation = '{"summary": "AI", "pages": [{"url": "https://a.com/docs", "section": "Docs", "description": "ai"}]}'

    def handler(request):
        url = str(request.url)
        if "openrouter.ai" in url:
            return httpx.Response(200, json={"choices": [{"message": {"content": curation}}]})
        return httpx.Response(200, text=MOCK[url]) if url in MOCK else httpx.Response(404, text="")

    monkeypatch.setattr(
        app_module, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    response = TestClient(app).post("/generate", json={"url": "https://a.com", "enhance": True})
    assert response.status_code == 200
    assert "ai" in response.json()["llms_txt"]

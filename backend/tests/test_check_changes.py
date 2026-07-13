import httpx
from fastapi.testclient import TestClient

import app as app_module
from app import app

SITEMAP = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://a.com/docs</loc></url></urlset>'


def _in_memory_db(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(app_module.db, "save_generation", lambda record: store.update({record["site_url"]: record}))
    monkeypatch.setattr(app_module.db, "load_generation", lambda site_url: store.get(site_url))
    return store


def test_check_changes_replays_persisted_honor_robots(monkeypatch):
    # A file generated with honor_robots=false must be re-checked the same way,
    # or the compliant re-run of a Disallow:/ site would see zero pages and
    # report a false "changed".
    store = _in_memory_db(monkeypatch)

    def handler(request):
        url = str(request.url)
        if url.endswith("robots.txt"):
            return httpx.Response(
                200, text="User-agent: *\nDisallow: /\nSitemap: https://a.com/sitemap.xml"
            )
        if url.endswith("sitemap.xml"):
            return httpx.Response(200, text=SITEMAP)
        return httpx.Response(200, text="<title>T</title>")

    monkeypatch.setattr(
        app_module, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    client = TestClient(app)

    generated = client.post("/generate", json={"url": "https://a.com", "honor_robots": False})
    assert generated.status_code == 200
    assert store["https://a.com"]["honor_robots"] is False

    recheck = client.post("/check-changes", json={"url": "https://a.com"}).json()
    assert recheck["changed"] is False


def test_check_changes_first_run_unchanged_then_changed(monkeypatch):
    _in_memory_db(monkeypatch)
    state = {"title": "Original"}

    def handler(request):
        url = str(request.url)
        if url.endswith("robots.txt"):
            return httpx.Response(200, text="Sitemap: https://a.com/sitemap.xml")
        if url.endswith("sitemap.xml"):
            return httpx.Response(200, text=SITEMAP)
        return httpx.Response(200, text=f"<title>{state['title']}</title>")

    monkeypatch.setattr(
        app_module, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    client = TestClient(app)

    first = client.post("/check-changes", json={"url": "https://a.com"}).json()
    assert first["changed"] is True
    assert first["details"] == "no prior generation"

    unchanged = client.post("/check-changes", json={"url": "https://a.com"}).json()
    assert unchanged["changed"] is False
    assert unchanged["regenerated_llms_txt"] is None
    assert unchanged["details"] == "no changes"

    state["title"] = "Updated"
    changed = client.post("/check-changes", json={"url": "https://a.com"}).json()
    assert changed["changed"] is True
    assert changed["regenerated_llms_txt"] is not None
    assert changed["details"] == "changed"


def test_check_changes_skips_upload_and_persist_on_empty_recrawl(monkeypatch):
    # A transient crawl failure (e.g. the site is briefly unreachable) must not
    # overwrite the durable S3 object at the stable key with a near-empty
    # document, nor persist a bogus DB row over the last-good one.
    _in_memory_db(monkeypatch)

    calls: list[str] = []
    monkeypatch.setattr(
        app_module.storage, "upload_llms_txt",
        lambda content, key: calls.append("upload") or "https://files.example/x.txt",
    )
    monkeypatch.setattr(
        app_module.db, "save_generation",
        lambda record: calls.append("save_generation"),
    )
    async def _empty_generate(*args, **kwargs):
        return {"llms_txt": "", "pages": [], "warnings": ["crawl failed"]}

    monkeypatch.setattr(app_module, "generate", _empty_generate)
    client = TestClient(app)

    response = client.post("/check-changes", json={"url": "https://a.com"})

    assert response.status_code == 200
    body = response.json()
    assert body["changed"] is False
    assert body["regenerated_llms_txt"] is None
    assert body["public_url"] is None
    assert "no pages fetched" in body["details"]
    assert calls == []

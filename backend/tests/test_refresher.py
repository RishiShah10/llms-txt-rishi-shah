import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

import app as app_module
import refresher
from app import app

pytestmark = pytest.mark.anyio

SITEMAP = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://a.com/docs</loc></url></urlset>'


def _sitemap_with_lastmod(lastmod: str) -> str:
    return (
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>https://a.com/docs</loc><lastmod>{lastmod}</lastmod></url></urlset>"
    )


def _prior_row(**overrides) -> dict:
    row = {
        "content_hash": "abc", "object_key": "a.com.txt", "public_url": "https://x/a.com.txt",
        "scope_prefix": None, "crawl": True, "max_pages": 50,
        "enhance": False, "bypass": False, "honor_robots": True,
        "sitemap_newest_lastmod": None,
    }
    row.update(overrides)
    return row


def _in_memory_db(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(app_module.db, "save_generation", lambda record: store.update({record["site_url"]: record}))
    monkeypatch.setattr(app_module.db, "load_generation", lambda site_url: store.get(site_url))
    return store


def _mock_site(monkeypatch, state):
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


def test_cron_refresh_fails_shut_without_configured_secret(monkeypatch):
    # No CRON_SECRET in the environment means the endpoint must reject
    # everything -- an unset secret is a closed door, not an open one.
    monkeypatch.delenv("CRON_SECRET", raising=False)
    client = TestClient(app)
    response = client.post("/internal/cron/refresh", headers={"x-cron-secret": "anything"})
    assert response.status_code == 401


def test_cron_refresh_rejects_wrong_or_missing_secret(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    client = TestClient(app)
    assert client.post("/internal/cron/refresh").status_code == 401
    assert client.post("/internal/cron/refresh", headers={"x-cron-secret": "wrong"}).status_code == 401


def test_cron_refresh_triggers_background_sweep(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    calls = []
    monkeypatch.setattr(refresher, "refresh_due_sites",
                        lambda factory, slots: calls.append((factory, slots)))
    client = TestClient(app)

    response = client.post("/internal/cron/refresh", headers={"x-cron-secret": "s3cret"})

    assert response.status_code == 200
    assert response.json() == {"status": "triggered"}
    # TestClient runs background tasks before returning the response.
    assert len(calls) == 1
    # The sweep re-generates sites with their stored settings, bypass=True and all.
    # It must contend for the same cap as the request paths, not run beside it.
    assert calls[0][1] is app_module._generation_slots


def test_generate_enrolls_site_with_clamped_interval(monkeypatch):
    _in_memory_db(monkeypatch)
    _mock_site(monkeypatch, {"title": "T"})
    enrolled = []
    monkeypatch.setattr(
        app_module.db, "enroll_auto_update",
        lambda site_url, interval_days: enrolled.append((site_url, interval_days)),
    )
    client = TestClient(app)

    ok = client.post("/generate", json={"url": "https://a.com", "auto_update": True,
                                        "recrawl_interval_days": 0})
    assert ok.status_code == 200
    # Zero/negative days clamp up to the 1-day floor.
    assert enrolled == [("https://a.com", 1)]

    client.post("/generate", json={"url": "https://a.com"})
    assert len(enrolled) == 1  # auto_update off -> no enrollment call


def test_refresh_updates_changed_site_and_reschedules(monkeypatch):
    store = _in_memory_db(monkeypatch)
    state = {"title": "Original"}
    _mock_site(monkeypatch, state)
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    monkeypatch.setattr(app_module.db, "load_due_sites", lambda: ["https://a.com"])
    rescheduled = []
    monkeypatch.setattr(app_module.db, "schedule_next_check", rescheduled.append)
    client = TestClient(app)

    assert client.post("/generate", json={"url": "https://a.com"}).status_code == 200
    hash_before = store["https://a.com"]["content_hash"]

    state["title"] = "Updated"
    client.post("/internal/cron/refresh", headers={"x-cron-secret": "s3cret"})

    assert store["https://a.com"]["content_hash"] != hash_before
    assert rescheduled == ["https://a.com"]


def test_refresh_unchanged_site_does_not_rewrite(monkeypatch):
    store = _in_memory_db(monkeypatch)
    _mock_site(monkeypatch, {"title": "Stable"})
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    monkeypatch.setattr(app_module.db, "load_due_sites", lambda: ["https://a.com"])
    client = TestClient(app)

    assert client.post("/generate", json={"url": "https://a.com"}).status_code == 200

    uploads = []
    monkeypatch.setattr(
        app_module.storage, "upload_llms_txt",
        lambda content, key: uploads.append(key) or "https://files.example/x.txt",
    )
    saved_before = dict(store["https://a.com"])
    client.post("/internal/cron/refresh", headers={"x-cron-secret": "s3cret"})

    assert uploads == []
    assert store["https://a.com"] == saved_before


async def test_refresh_site_skips_on_empty_recrawl(monkeypatch):
    # Transient crawl failure: leave the stored file and row alone (the same
    # no-clobber stance /check-changes takes) and report "skipped".
    saves = []
    async def _no_lastmod(*args):
        return None
    monkeypatch.setattr(refresher, "_newest_sitemap_lastmod", _no_lastmod)
    monkeypatch.setattr(refresher.db, "load_generation", lambda site_url: _prior_row())
    monkeypatch.setattr(refresher.db, "save_generation", saves.append)
    async def _empty_generate(*args, **kwargs):
        return {"llms_txt": "", "pages": [], "warnings": ["down"]}
    monkeypatch.setattr(refresher, "generate", _empty_generate)

    outcome = await refresher.refresh_site("https://a.com", lambda: httpx.AsyncClient())

    assert outcome == "skipped"
    assert saves == []


async def test_refresh_keeps_prior_pointer_when_upload_fails(monkeypatch):
    # If S3 is briefly unavailable the row must keep pointing at the last-good
    # object instead of nulling out object_key/public_url.
    saves = []
    async def _no_lastmod(*args):
        return None
    monkeypatch.setattr(refresher, "_newest_sitemap_lastmod", _no_lastmod)
    monkeypatch.setattr(refresher.db, "load_generation",
                        lambda site_url: _prior_row(content_hash="stale"))
    monkeypatch.setattr(refresher.db, "save_generation", saves.append)
    async def _one_page_generate(*args, **kwargs):
        return {
            "llms_txt": "# A", "warnings": [],
            "pages": [{"url": "https://a.com/docs", "title": "Docs", "description": None}],
        }
    monkeypatch.setattr(refresher, "generate", _one_page_generate)

    def _boom(content, key):
        raise RuntimeError("s3 down")
    monkeypatch.setattr(refresher.storage, "upload_llms_txt", _boom)

    outcome = await refresher.refresh_site("https://a.com", lambda: httpx.AsyncClient())

    assert outcome == "changed"
    assert saves[0]["object_key"] == "a.com.txt"
    assert saves[0]["public_url"] == "https://x/a.com.txt"


def _gate_client_factory(lastmod: str, requests: list[str]):
    def handler(request):
        url = str(request.url)
        requests.append(url)
        if url.endswith("robots.txt"):
            return httpx.Response(200, text="Sitemap: https://a.com/sitemap.xml")
        if url.endswith("sitemap.xml"):
            return httpx.Response(200, text=_sitemap_with_lastmod(lastmod))
        return httpx.Response(200, text="<title>T</title>")

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_gate_skips_full_crawl_when_sitemap_lastmod_unchanged(monkeypatch):
    # The whole point of the gate: a due site whose sitemap hasn't advanced
    # costs two XML fetches (robots + sitemap), not a page-by-page crawl.
    baseline = datetime(2026, 7, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(refresher.db, "load_generation",
                        lambda site_url: _prior_row(sitemap_newest_lastmod=baseline))

    async def _must_not_run(*args, **kwargs):
        raise AssertionError("full pipeline ran despite fresh sitemap")
    monkeypatch.setattr(refresher, "generate", _must_not_run)

    requests: list[str] = []
    outcome = await refresher.refresh_site(
        "https://a.com", _gate_client_factory("2026-07-01T00:00:00Z", requests))

    assert outcome == "unchanged"
    assert all(url.endswith(("robots.txt", "sitemap.xml")) for url in requests)


async def test_gate_advanced_lastmod_triggers_full_run_and_records_baseline(monkeypatch):
    baseline = datetime(2026, 7, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(refresher.db, "load_generation",
                        lambda site_url: _prior_row(sitemap_newest_lastmod=baseline))
    saves, recorded = [], []
    monkeypatch.setattr(refresher.db, "save_generation", saves.append)
    monkeypatch.setattr(refresher.db, "record_sitemap_lastmod",
                        lambda site_url, lastmod: recorded.append(lastmod))

    requests: list[str] = []
    outcome = await refresher.refresh_site(
        "https://a.com", _gate_client_factory("2026-07-08T00:00:00Z", requests))

    assert outcome == "changed"
    assert saves  # full run persisted the new content
    assert recorded == [datetime(2026, 7, 8, tzinfo=timezone.utc)]


async def test_gate_inconclusive_on_first_refresh_falls_through_and_seeds_baseline(monkeypatch):
    # No stored baseline yet: must do the full crawl, then seed the baseline
    # so the *next* sweep can gate-skip.
    monkeypatch.setattr(refresher.db, "load_generation", lambda site_url: _prior_row())
    recorded = []
    monkeypatch.setattr(refresher.db, "record_sitemap_lastmod",
                        lambda site_url, lastmod: recorded.append(lastmod))

    requests: list[str] = []
    await refresher.refresh_site("https://a.com", _gate_client_factory("2026-07-01", requests))

    assert any(url.endswith("/docs") for url in requests)  # pages were fetched
    assert recorded == [datetime(2026, 7, 1, tzinfo=timezone.utc)]


async def test_full_run_reuses_probe_responses_instead_of_refetching(monkeypatch):
    # The probe and the full pipeline both want robots.txt + sitemap.xml; the
    # memo client means the site serves each exactly once per refresh.
    monkeypatch.setattr(refresher.db, "load_generation", lambda site_url: _prior_row())
    monkeypatch.setattr(refresher.db, "save_generation", lambda record: None)

    requests: list[str] = []
    await refresher.refresh_site("https://a.com", _gate_client_factory("2026-07-01", requests))

    assert requests.count("https://a.com/robots.txt") == 1
    assert requests.count("https://a.com/sitemap.xml") == 1
    assert any(url.endswith("/docs") for url in requests)  # full run still crawled


def test_parse_lastmod_is_timezone_correct():
    # Date-only values become UTC-aware, not naive.
    assert refresher._parse_lastmod("2026-07-01") == datetime(2026, 7, 1, tzinfo=timezone.utc)
    # Z suffix parses; offsets are honored in comparisons: 09:00+05:00 is
    # 04:00Z, so it sorts BEFORE 05:00Z -- naive comparison would invert this.
    z = refresher._parse_lastmod("2026-07-01T05:00:00Z")
    offset = refresher._parse_lastmod("2026-07-01T09:00:00+05:00")
    assert offset < z
    assert refresher._parse_lastmod("not-a-date") is None
    assert refresher._parse_lastmod(None) is None


async def test_probe_memo_client_passes_post_through():
    # refresh_site hands _ProbeMemoClient straight into generate(), which calls
    # curate(site, client, ...) -> client.post(...) when enhance=True. Without a
    # post passthrough that's an AttributeError, swallowed by curator.py's bare
    # except into a bogus "AI enhancement skipped" warning.
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as inner:
        client = refresher._ProbeMemoClient(inner)
        response = await client.post("https://api.example.com/v1/chat", json={})

    assert response.status_code == 200
    assert calls == ["https://api.example.com/v1/chat"]


async def test_probe_memo_client_dedupes_concurrent_gets():
    # Once fetching is concurrent, two coroutines can miss the cache for the
    # same sitemap URL and both hit the network -- defeating the whole point of
    # this class ("the site is hit once for those, not twice").
    hits = []

    async def handler(request):
        hits.append(str(request.url))
        await asyncio.sleep(0.01)  # hold the request open so the second overlaps
        return httpx.Response(200, text="ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as inner:
        client = refresher._ProbeMemoClient(inner)
        await asyncio.gather(
            client.get("https://a.com/sitemap.xml"),
            client.get("https://a.com/sitemap.xml"),
        )

    assert hits == ["https://a.com/sitemap.xml"], "the same URL was fetched twice"


async def test_refresh_sweep_survives_a_failing_site(monkeypatch):
    monkeypatch.setattr(refresher.db, "load_due_sites", lambda: ["https://bad.com", "https://good.com"])
    rescheduled = []
    monkeypatch.setattr(refresher.db, "schedule_next_check", rescheduled.append)

    async def fake_refresh(site_url, factory, slots=None):
        if "bad" in site_url:
            raise RuntimeError("boom")
        return "unchanged"
    monkeypatch.setattr(refresher, "refresh_site", fake_refresh)

    counts = await refresher.refresh_due_sites(lambda: httpx.AsyncClient())

    assert counts == {"due": 2, "changed": 0, "unchanged": 1, "skipped": 1}
    # Both sites reschedule -- a failing site must not stay permanently due.
    assert rescheduled == ["https://bad.com", "https://good.com"]


async def test_sweep_logs_its_progress(monkeypatch, caplog):
    # The nightly sweep runs in the background and was completely silent -- so
    # triggering it showed nothing. It now narrates itself, so `aws logs tail`
    # (and a live demo) can watch it work.
    import logging
    monkeypatch.setattr(refresher.db, "load_due_sites", lambda: ["https://a.com", "https://b.com"])
    monkeypatch.setattr(refresher.db, "load_generation", lambda url: None)  # no prior -> skipped
    monkeypatch.setattr(refresher.db, "schedule_next_check", lambda url: None)

    with caplog.at_level(logging.INFO, logger="uvicorn.refresh"):
        counts = await refresher.refresh_due_sites(lambda: httpx.AsyncClient())

    text = "\n".join(r.message for r in caplog.records)
    assert "2 site(s) due" in text
    assert "https://a.com -> skipped" in text
    assert "2 due, 0 changed, 0 unchanged, 2 skipped" in text
    assert counts == {"due": 2, "changed": 0, "unchanged": 0, "skipped": 2}

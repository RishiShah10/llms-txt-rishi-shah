import httpx
import pytest

import curator
from models import PageInfo, SiteData

pytestmark = pytest.mark.anyio


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _site():
    return SiteData(
        title="Acme",
        summary="raw summary",
        sections={"Pages": [PageInfo(url="https://a.com/x", title="X", description="raw")]},
        warnings=[],
    )


def _completion(content: str):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


async def test_curate_applies_llm_sections_and_descriptions(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    content = '{"summary": "clean summary", "pages": [{"url": "https://a.com/x", "section": "Docs", "description": "clean desc"}]}'
    warnings = []
    result = await curator.curate(_site(), _client(lambda req: _completion(content)), warnings)
    assert result.summary == "clean summary"
    assert "Docs" in result.sections
    assert result.sections["Docs"][0].description == "clean desc"
    assert warnings == []


async def test_curate_skips_without_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    called = []

    def handler(req):
        called.append(req)
        return _completion("{}")

    warnings = []
    site = _site()
    result = await curator.curate(site, _client(handler), warnings)
    assert result is site           # unchanged
    assert not called               # no HTTP call was made
    assert any("OPENROUTER_API_KEY" in w for w in warnings)


async def test_curate_falls_back_on_http_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    warnings = []
    site = _site()
    result = await curator.curate(site, _client(lambda req: httpx.Response(500, text="err")), warnings)
    assert result is site
    assert any("AI enhancement skipped" in w for w in warnings)


async def test_curate_falls_back_on_bad_json(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    warnings = []
    site = _site()
    result = await curator.curate(site, _client(lambda req: _completion("not json")), warnings)
    assert result is site
    assert warnings


async def test_curate_keeps_heuristic_for_dropped_pages(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    site = SiteData(
        title="Acme", summary="s",
        sections={
            "Docs": [PageInfo(url="https://a.com/a", title="A", description="da")],
            "Pages": [PageInfo(url="https://a.com/b", title="B", description="db")],
        },
        warnings=[],
    )
    content = '{"summary": "", "pages": [{"url": "https://a.com/a", "section": "Guides", "description": "new a"}]}'
    warnings = []
    result = await curator.curate(site, _client(lambda req: _completion(content)), warnings)
    assert any(p.url == "https://a.com/a" and p.description == "new a" for p in result.sections.get("Guides", []))
    assert any(p.url == "https://a.com/b" and p.description == "db" for p in result.sections.get("Pages", []))
    assert result.summary == "s"     # empty LLM summary → keep original


async def test_curate_falls_back_on_non_dict_json(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    warnings = []
    site = _site()
    result = await curator.curate(site, _client(lambda req: _completion("[1, 2, 3]")), warnings)
    assert result is site
    assert warnings


async def test_curate_http_error_warning_is_single_line(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    warnings = []
    await curator.curate(_site(), _client(lambda req: httpx.Response(500, text="err")), warnings)
    assert warnings and "\n" not in warnings[0]

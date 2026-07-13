import httpx
import pytest

from generator import generate
from progress import Event

pytestmark = pytest.mark.anyio

_SITEMAP = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://a.com/one</loc></url>
<url><loc>https://a.com/two</loc></url>
</urlset>"""

_PAGE = "<html><head><title>T</title></head><body><p>Hi</p></body></html>"


def _sitemap_site(request):
    path = request.url.path
    if path == "/robots.txt":
        return httpx.Response(404)
    if path == "/sitemap.xml":
        return httpx.Response(200, text=_SITEMAP)
    if request.method == "HEAD":
        return httpx.Response(404)
    return httpx.Response(200, text=_PAGE)


async def _run(handler, **kwargs) -> list[Event]:
    events: list[Event] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await generate("https://a.com", client, on_event=events.append, **kwargs)
    return events


async def test_stage_sequence():
    events = await _run(_sitemap_site)
    stages = [event.stage for event in events]
    # The pipeline's phases are sequential, so these must appear in this order.
    for earlier, later in [("robots", "home"), ("home", "discover"),
                           ("discover", "extract"), ("extract", "rank"),
                           ("rank", "format"), ("format", "validate")]:
        assert stages.index(earlier) < stages.index(later), f"{earlier} did not precede {later}"


async def test_every_start_has_a_matching_done():
    events = await _run(_sitemap_site)
    starts = [(e.stage, e.url) for e in events if e.phase == "start"]
    dones = [(e.stage, e.url) for e in events if e.phase == "done" and e.stage != "page"]
    assert starts, "no start frames were emitted at all"
    assert sorted(starts) == sorted(dones)


async def test_robots_and_homepage_fetches_are_instrumented():
    # These are fetched OUTSIDE the four loops -- an earlier design missed them
    # entirely, leaving the log dead for the first several seconds.
    events = await _run(_sitemap_site)
    assert any(e.stage == "robots" and e.phase == "start" for e in events)
    assert any(e.stage == "home" and e.phase == "start" for e in events)


async def test_page_counter_is_monotonic_and_carries_a_total():
    events = await _run(_sitemap_site)
    counts = [e.done for e in events if e.stage == "page"]
    assert counts == sorted(counts), "the admitted-page counter went backwards"
    assert counts, "no page frames were emitted"
    assert all(e.total is not None for e in events if e.stage == "page")


async def test_counter_moves_on_a_crawl_only_site_with_no_sitemap():
    # THE REGRESSION THIS TASK EXISTS TO PREVENT.
    # With no sitemap, every candidate comes from the BFS crawl already extracted
    # (it has a title), and _extract_pages deliberately does NOT re-fetch those.
    # A counter keyed on fetch-completions therefore reads 0/N for the whole run.
    # The counter must count pages ADMITTED, not fetches completed.
    home = '<html><body><a href="https://a.com/x">x</a><a href="https://a.com/y">y</a></body></html>'

    def crawl_only(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path.endswith(".xml"):
            return httpx.Response(404)     # no sitemap anywhere
        if request.method == "HEAD":
            return httpx.Response(404)
        if request.url.path in ("/", ""):
            return httpx.Response(200, text=home)
        return httpx.Response(200, text=_PAGE)

    events = await _run(crawl_only, crawl=True)
    counts = [e.done for e in events if e.stage == "page"]
    assert counts, "no page frames on a crawl-only site -- the counter would show 0/N"
    assert max(counts) >= 2


async def test_a_raising_emitter_cannot_change_the_output():
    # Instrumentation must never affect the document. A raising emitter inside
    # fetch_one would otherwise be swallowed by _extract_pages' return_exceptions
    # collector and silently drop that page.
    def exploding(event):
        raise RuntimeError("emitter blew up")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_sitemap_site)) as client:
        quiet = await generate("https://a.com", client)
        loud = await generate("https://a.com", client, on_event=exploding)

    assert loud["llms_txt"] == quiet["llms_txt"]
    assert loud["warnings"] == quiet["warnings"]
    assert len(loud["pages"]) == len(quiet["pages"])


async def test_run_generation_emits_a_persist_stage(monkeypatch):
    # The S3 upload + Neon write is a couple of seconds of silence right before
    # the result lands. It must announce itself, like every other blocking step.
    # (conftest.py already stubs db.* and storage.* for the whole suite.)
    import app as app_mod

    monkeypatch.setattr(
        app_mod, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_sitemap_site)),
    )

    events: list[Event] = []
    request = app_mod.GenerateRequest(url="https://a.com", crawl=False)
    await app_mod._run_generation(request, events.append)

    persist = [event.phase for event in events if event.stage == "persist"]
    assert persist == ["start", "done"]


async def test_a_raising_emitter_cannot_break_persistence(monkeypatch):
    # app.py's persist frames used to call on_event RAW, bypassing the emit()
    # guard the generator uses -- the one hole in "instrumentation can never
    # change the output". A raising emitter there would abort the upload and
    # report an error for a generation that actually SUCCEEDED.
    import app as app_mod

    monkeypatch.setattr(
        app_mod, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_sitemap_site)),
    )
    stored: list = []
    monkeypatch.setattr(app_mod, "_store_file",
                        lambda *args: (stored.append(args) or ("key", "https://url")))

    def exploding(event):
        raise RuntimeError("emitter blew up")

    request = app_mod.GenerateRequest(url="https://a.com", crawl=False)
    result = await app_mod._run_generation(request, exploding)

    assert stored, "the upload never ran -- a raising emitter aborted persistence"
    assert result["public_url"] == "https://url"


async def test_run_generation_raises_422_on_a_zero_page_site(monkeypatch):
    # The WS handler must CATCH this -- raising HTTPException inside an accepted
    # websocket kills the connection instead of producing an error frame.
    import app as app_mod
    from fastapi import HTTPException

    monkeypatch.setattr(
        app_mod, "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )

    request = app_mod.GenerateRequest(url="https://a.com", crawl=False)
    with pytest.raises(HTTPException) as raised:
        await app_mod._run_generation(request, lambda event: None)
    assert raised.value.status_code == 422


async def test_a_twin_probe_that_is_not_markdown_reports_not_found():
    # THE LYING TICK. An SPA answers EVERY path with its HTML shell and a 200, so
    # resy.com returned 200 for /privacy.md, /careers.md and every other guess.
    # _probe_md_twins correctly rejects them on content-type -- but the live log
    # showed a green "✓ 200" against 26 twins that do not exist, and the document
    # linked to none of them. For a twin probe, `ok` must mean "a twin was found".
    def spa(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=_SITEMAP)
        if request.method == "HEAD":
            # The SPA shell: 200, but HTML -- not a markdown twin.
            return httpx.Response(200, headers={"content-type": "text/html"})
        return httpx.Response(200, text=_PAGE)

    events = await _run(spa)
    twins = [e for e in events if e.stage == "twins" and e.phase == "done"]
    assert twins, "no twin probes ran"
    assert all(e.status == 200 for e in twins), "precondition: the SPA answers 200"
    assert not any(e.ok for e in twins), "a 200 that isn't markdown is NOT a twin"


async def test_a_real_markdown_twin_reports_found():
    def docs_host(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=_SITEMAP)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-type": "text/markdown"})
        return httpx.Response(200, text=_PAGE)

    events = await _run(docs_host)
    twins = [e for e in events if e.stage == "twins" and e.phase == "done"]
    assert twins and all(e.ok for e in twins), "a real markdown twin must report found"

from models import PageInfo, SiteData
from formatter import format_llms_txt
from validator import validate


def test_minimal_file_has_h1():
    site = SiteData(title="Acme", summary=None, sections={}, warnings=[])
    assert format_llms_txt(site).splitlines()[0] == "# Acme"


def test_summary_and_sections_in_order():
    site = SiteData(
        title="Acme",
        summary="Acme is a demo.",
        sections={
            "Docs": [PageInfo(url="https://a.com/q", title="Quickstart", description="start")],
            "Optional": [PageInfo(url="https://a.com/legal", title="Legal")],
        },
        warnings=[],
    )
    out = format_llms_txt(site)
    assert out.startswith("# Acme\n\n> Acme is a demo.\n")
    assert "## Docs\n- [Quickstart](https://a.com/q): start" in out
    assert out.index("## Docs") < out.index("## Optional")
    assert out.rstrip().endswith("- [Legal](https://a.com/legal)")


def test_link_prefers_md_twin_url():
    # A verified markdown twin is the better link target for LLM readers,
    # but the canonical URL stays on the PageInfo.
    site = SiteData(
        title="Acme",
        summary=None,
        sections={
            "Docs": [
                PageInfo(url="https://a.com/q", title="Quickstart",
                         description="start", md_url="https://a.com/q.md"),
                PageInfo(url="https://a.com/api", title="API"),
            ],
        },
        warnings=[],
    )
    out = format_llms_txt(site)
    assert "- [Quickstart](https://a.com/q.md): start" in out
    assert "- [API](https://a.com/api)" in out


def test_brackets_and_newlines_in_titles_are_sanitized():
    # Raw brackets/newlines in titles or descriptions would otherwise produce
    # a link line the validator itself rejects (e.g. "- [Guide [beta]](url)").
    site = SiteData(
        title="Acme [beta]",
        summary="We do\nthings [fast]",
        sections={
            "Docs": [
                PageInfo(
                    url="https://a.com/q",
                    title="Guide [beta]\nEdition",
                    description="start\nhere [now]",
                )
            ],
        },
        warnings=[],
    )
    out = format_llms_txt(site)
    result = validate(out)
    assert result.ok, result.errors

from models import PageInfo
from ranker import rank


def test_groups_by_path_and_routes_optional():
    pages = [
        PageInfo(url="https://a.com/docs/start", title="Start"),
        PageInfo(url="https://a.com/guides/x", title="Guide X"),
        PageInfo(url="https://a.com/privacy", title="Privacy"),
        PageInfo(url="https://a.com/", title="Home"),
    ]
    site = rank(pages, title="Acme", summary="demo")
    assert "Docs" in site.sections
    assert "Guides" in site.sections
    assert any(p.title == "Privacy" for p in site.sections["Optional"])
    assert not any(p.title == "Home" for p in site.sections.get("Optional", []))


def test_sections_fan_out_across_a_real_site_shape():
    # The point of the segment map: a site crawled at its root should break into
    # a few meaningful buckets, not one undifferentiated "Pages" list.
    pages = [
        PageInfo(url="https://a.com/", title="Home"),
        PageInfo(url="https://a.com/docs/install", title="Install"),
        PageInfo(url="https://a.com/guides/quickstart", title="Quickstart"),
        PageInfo(url="https://a.com/learn/basics", title="Basics"),
        PageInfo(url="https://a.com/reference/cli", title="CLI"),
        PageInfo(url="https://a.com/sdk/python", title="Python SDK"),
        PageInfo(url="https://a.com/changelog", title="Changelog"),
        PageInfo(url="https://a.com/pricing", title="Pricing"),
        PageInfo(url="https://a.com/examples/todo", title="Todo example"),
        PageInfo(url="https://a.com/community/forum", title="Forum"),
        PageInfo(url="https://a.com/careers", title="Careers"),
        PageInfo(url="https://a.com/privacy", title="Privacy"),
    ]
    site = rank(pages, title="A", summary=None)

    assert set(site.sections) == {
        "Pages", "Docs", "Guides", "API", "Blog", "Product", "Examples",
        "Community", "Optional",
    }
    # /learn and /guides both mean "task-oriented"; /reference and /sdk both mean
    # "machine-facing" -- vocabulary, not separate buckets.
    assert len(site.sections["Guides"]) == 2
    assert len(site.sections["API"]) == 2
    # Legal + company pages are skippable, per the spec's Optional section.
    assert len(site.sections["Optional"]) == 2


def test_a_flat_site_honestly_lands_in_pages():
    # A site with no structure in its URLs has no structure to find. Falling back
    # to one bucket is correct, not a failure.
    pages = [
        PageInfo(url="https://a.com/billing", title="Billing"),
        PageInfo(url="https://a.com/cli", title="CLI"),
    ]
    site = rank(pages, title="A", summary=None)
    assert set(site.sections) == {"API", "Pages"}   # /cli is machine-facing

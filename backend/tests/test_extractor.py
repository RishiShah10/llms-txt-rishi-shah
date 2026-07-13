from extractor import extract, DESC_LIMIT


def test_extracts_title_and_meta_description():
    html = """
    <html><head>
      <title>Quickstart - Acme</title>
      <meta name="description" content="Get started in 5 minutes">
    </head><body><h1>Quickstart</h1></body></html>
    """
    page = extract(html, "https://a.com/quickstart")
    assert page.title == "Quickstart - Acme"
    assert page.description == "Get started in 5 minutes"
    assert page.url == "https://a.com/quickstart"


def test_falls_back_to_h1_and_first_paragraph():
    html = "<html><body><h1>Guide</h1><p>Intro text.</p></body></html>"
    page = extract(html, "https://a.com/guide")
    assert page.title == "Guide"
    assert page.description == "Intro text."


def test_title_falls_back_to_url_slug_when_empty():
    page = extract("<html></html>", "https://a.com/pricing")
    assert page.title == "pricing"
    assert page.description is None


def test_long_description_is_capped():
    long_para = "word " * 300  # ~1500 chars, well over the limit
    html = f"<html><body><h1>T</h1><p>{long_para}</p></body></html>"
    page = extract(html, "https://a.com/x")
    assert page.description.endswith("…")
    assert len(page.description) <= DESC_LIMIT + 1


def test_markdown_page_uses_heading_and_first_prose_line_not_whole_body():
    # A .md twin is Markdown, not HTML — the old code dumped the whole file as the
    # description. Now: title from the heading, description from the first prose line.
    md = "# The /llms.txt file\nFirst real line of prose.\n## Section\n" + ("blah " * 500)
    page = extract(md, "https://a.com/doc.md")
    assert page.title == "The /llms.txt file"
    assert page.description == "First real line of prose."
    assert "blah" not in (page.description or "")

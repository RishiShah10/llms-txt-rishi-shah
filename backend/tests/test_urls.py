from urls import (canonical, has_js_placeholder, md_twin_of, normalize, is_asset,
                  is_disallowed, same_origin_links)


def test_md_twin_of_appends_md_to_plain_paths():
    assert md_twin_of("https://a.com/docs/page") == "https://a.com/docs/page.md"


def test_md_twin_of_html_pages():
    assert md_twin_of("https://a.com/p.html") == "https://a.com/p.html.md"


def test_md_twin_of_directory_and_root():
    assert md_twin_of("https://a.com/docs/") == "https://a.com/docs/index.html.md"
    assert md_twin_of("https://a.com/") == "https://a.com/index.html.md"
    assert md_twin_of("https://a.com") == "https://a.com/index.html.md"


def test_md_twin_of_strips_query_and_fragment():
    assert md_twin_of("https://a.com/p?id=2#top") == "https://a.com/p.md"


def test_allow_carves_exception_from_disallow():
    # RFC 9309 §2.2.2: the most specific (longest) matching rule wins.
    disallow, allow = ["/search"], ["/search/about"]
    assert is_disallowed("https://a.com/search/x", disallow, allow)
    assert not is_disallowed("https://a.com/search/about", disallow, allow)
    assert not is_disallowed("https://a.com/search/about/team", disallow, allow)
    assert not is_disallowed("https://a.com/docs", disallow, allow)


def test_allow_wins_length_ties():
    assert not is_disallowed("https://a.com/p", ["/p"], ["/p"])


def test_allow_with_wildcard_rules():
    disallow, allow = ["/docs/*"], ["/docs/public"]
    assert is_disallowed("https://a.com/docs/private", disallow, allow)
    assert not is_disallowed("https://a.com/docs/public", disallow, allow)


def test_disallow_without_allow_still_blocks():
    assert is_disallowed("https://a.com/admin/x", ["/admin/"])


def test_normalize_strips_fragment_and_trailing_slash():
    assert normalize("https://A.com/Docs/#top") == "https://a.com/Docs"
    assert normalize("https://a.com/docs/") == "https://a.com/docs"
    assert normalize("https://a.com/") == "https://a.com/"


def test_normalize_keeps_query():
    assert normalize("https://a.com/p?id=2") == "https://a.com/p?id=2"


def test_is_asset_detects_files():
    assert is_asset("https://a.com/file.pdf")
    assert is_asset("https://a.com/img.PNG")
    assert not is_asset("https://a.com/docs")


def test_same_origin_links_filters():
    html = """
      <a href="/docs">docs</a>
      <a href="https://other.com/x">ext</a>
      <a href="mailto:a@b.com">mail</a>
      <a href="/file.pdf">pdf</a>
      <a href="#frag">frag</a>
      <a href="https://a.com/guide#sec">guide</a>
    """
    links = same_origin_links(html, "https://a.com/", "https://a.com")
    assert "https://a.com/docs" in links
    assert "https://a.com/guide" in links
    assert all("other.com" not in link for link in links)
    assert all(not link.endswith(".pdf") for link in links)


def test_same_origin_links_resolves_relative_to_page():
    html = '<a href="intro">i</a> <a href="../top">t</a>'
    links = same_origin_links(html, "https://a.com/docs/guide/", "https://a.com")
    assert "https://a.com/docs/guide/intro" in links
    assert "https://a.com/docs/top" in links


def test_disallow_plain_prefix_still_matches():
    assert is_disallowed("https://a.com/admin/x", ["/admin/"])
    assert not is_disallowed("https://a.com/public", ["/admin/"])


def test_disallow_root_blocks_everything():
    assert is_disallowed("https://a.com/", ["/"])
    assert is_disallowed("https://a.com/any/page", ["/"])


def test_disallow_wildcard_matches_any_characters():
    assert is_disallowed("https://a.com/private/x", ["/private/*"])
    assert is_disallowed("https://a.com/api/v2/edit", ["/api/*/edit"])
    assert not is_disallowed("https://a.com/api/v2/view", ["/api/*/edit"])


def test_disallow_dollar_anchors_to_end():
    assert is_disallowed("https://a.com/file.pdf", ["/*.pdf$"])
    assert not is_disallowed("https://a.com/file.pdf.html", ["/*.pdf$"])
    assert is_disallowed("https://a.com/docs", ["/docs$"])
    assert not is_disallowed("https://a.com/docs/intro", ["/docs$"])


def test_disallow_matches_query_string():
    assert is_disallowed("https://a.com/search?q=x", ["/search?q="])
    assert not is_disallowed("https://a.com/search", ["/search?q="])


def test_tracking_params_are_stripped_from_the_emitted_url():
    # tailwindcss.com stamps its OWN internal links: /plus?ref=footer and
    # /plus?ref=sidebar. Without stripping, the same page is crawled and listed
    # three times, each with a tracking param in the URL.
    assert canonical("https://a.com/plus?ref=footer") == "https://a.com/plus"
    assert canonical("https://a.com/p?utm_source=x&utm_campaign=y") == "https://a.com/p"


def test_tracking_params_do_not_split_one_page_into_several():
    assert normalize("https://a.com/plus?ref=footer") == normalize("https://a.com/plus?ref=sidebar")
    assert normalize("https://a.com/plus?ref=footer") == normalize("https://a.com/plus")


def test_a_real_query_param_is_kept():
    # Conservative on purpose: a generic-looking param can be load-bearing, and
    # dropping it would merge two genuinely different pages.
    assert canonical("https://a.com/p?id=7") == "https://a.com/p?id=7"
    assert normalize("https://a.com/p?id=7") != normalize("https://a.com/p?id=8")


def test_query_param_order_does_not_split_a_page():
    assert normalize("https://a.com/p?b=2&a=1") == normalize("https://a.com/p?a=1&b=2")


def test_crawled_links_come_back_canonical():
    html = '<a href="/plus?ref=footer">x</a><a href="/plus?ref=sidebar">y</a>'
    links = same_origin_links(html, "https://a.com/", "https://a.com")
    assert links == ["https://a.com/plus", "https://a.com/plus"]


def test_js_placeholder_urls_are_not_crawled():
    # A rendered SPA can link to /cities/undefined/... when its JS interpolates a
    # variable it never set (resy.com does exactly this). Always a dead end, and on
    # these sites every fetch is a PAID browser render.
    html = '<a href="/cities/undefined/x">a</a><a href="/cities/nyc/x">b</a>'
    assert same_origin_links(html, "https://a.com/", "https://a.com") == ["https://a.com/cities/nyc/x"]


def test_a_word_merely_containing_undefined_is_kept():
    # Whole segments only -- /docs/undefined-behavior is a real page.
    assert not has_js_placeholder("https://a.com/docs/undefined-behavior")
    assert has_js_placeholder("https://a.com/cities/undefined/x")

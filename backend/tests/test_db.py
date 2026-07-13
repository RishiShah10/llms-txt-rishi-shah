from db import site_hash


def test_site_hash_deterministic_and_order_independent():
    pages = [
        {"url": "https://a.com/x", "title": "X", "description": "dx"},
        {"url": "https://a.com/y", "title": "Y", "description": "dy"},
    ]
    assert site_hash(pages) == site_hash(list(reversed(pages)))


def test_site_hash_changes_on_content_or_structure():
    base = [{"url": "https://a.com/x", "title": "X", "description": "dx"}]
    assert site_hash(base) != site_hash([{**base[0], "title": "X2"}])         # title changed
    assert site_hash(base) != site_hash([{**base[0], "description": "dx2"}])   # description changed
    assert site_hash(base) != site_hash(base + [{"url": "https://a.com/y", "title": "Y"}])  # page added


def test_site_hash_handles_missing_description():
    with_none = site_hash([{"url": "https://a.com/x", "title": "X", "description": None}])
    with_empty = site_hash([{"url": "https://a.com/x", "title": "X", "description": ""}])
    assert with_none == with_empty

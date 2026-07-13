from validator import validate


def test_valid_document_passes():
    doc = "# Acme\n\n> summary\n\n## Docs\n- [Quickstart](https://a.com/q): start\n"
    assert validate(doc).ok


def test_missing_h1_fails():
    result = validate("> no title here\n")
    assert not result.ok
    assert any("H1" in e for e in result.errors)


def test_malformed_link_fails():
    result = validate("# Acme\n\n## Docs\n- Quickstart https://a.com/q\n")
    assert not result.ok
    assert any("link" in e.lower() for e in result.errors)

import storage


def test_object_key_strips_scheme_and_appends_txt():
    assert storage.object_key_for("https://llmstxt.org") == "llmstxt.org.txt"
    assert storage.object_key_for("http://example.com/") == "example.com.txt"


def test_object_key_preserves_path_structure():
    assert storage.object_key_for("https://fastapi.tiangolo.com/tutorial") == "fastapi.tiangolo.com/tutorial.txt"


def test_object_key_sanitizes_unsafe_chars():
    assert storage.object_key_for("https://ex.com/a b?x=1") == "ex.com/a-b-x-1.txt"


def test_public_url_for(monkeypatch):
    monkeypatch.setattr(storage, "_BUCKET", "my-bucket")
    monkeypatch.setattr(storage, "_REGION", "us-east-2")
    assert storage.public_url_for("llmstxt.org.txt") == "https://my-bucket.s3.us-east-2.amazonaws.com/llmstxt.org.txt"


def test_upload_puts_object_and_returns_url(monkeypatch):
    # Undo the conftest-wide autouse stub of storage.upload_llms_txt (needed so
    # app-level tests don't need real AWS) — this test exercises the real
    # implementation.
    monkeypatch.undo()
    calls = {}

    class FakeS3:
        def put_object(self, **kw):
            calls.update(kw)

    monkeypatch.setattr(storage, "_BUCKET", "my-bucket")
    monkeypatch.setattr(storage, "_REGION", "us-east-2")
    monkeypatch.setattr(storage, "_s3", lambda: FakeS3())

    url = storage.upload_llms_txt("# hello", "llmstxt.org.txt")
    assert url == "https://my-bucket.s3.us-east-2.amazonaws.com/llmstxt.org.txt"
    assert calls["Bucket"] == "my-bucket"
    assert calls["Key"] == "llmstxt.org.txt"
    assert calls["Body"] == b"# hello"
    assert calls["ContentType"].startswith("text/plain")

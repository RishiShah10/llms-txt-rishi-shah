import pytest

import db
import storage


@pytest.fixture(autouse=True)
def _no_live_db(monkeypatch):
    # Guarantee the suite never touches real Postgres: stub persistence by
    # default. Tests that exercise the change-detection flow override these with
    # their own in-memory store.
    #
    # enroll_auto_update's stub is a named function, not a lambda: app.py passes
    # db.enroll_auto_update directly to run_in_threadpool, and
    # test_threadpool_offload.py identifies offloaded calls by __name__ -- a
    # lambda would show up as "<lambda>" and break that assertion.
    def enroll_auto_update(site_url, interval_days):
        return None

    monkeypatch.setattr(db, "save_generation", lambda record: None)
    monkeypatch.setattr(db, "load_generation", lambda site_url: None)
    monkeypatch.setattr(db, "init_db", lambda: None)
    monkeypatch.setattr(db, "enroll_auto_update", enroll_auto_update)
    monkeypatch.setattr(db, "load_due_sites", lambda: [])
    monkeypatch.setattr(db, "schedule_next_check", lambda site_url: None)
    monkeypatch.setattr(db, "record_sitemap_lastmod", lambda site_url, lastmod: None)


@pytest.fixture(autouse=True)
def _stub_storage(monkeypatch):
    # Guarantee the suite never touches real S3: stub the upload by default so
    # handler tests don't need AWS credentials or a real bucket.
    monkeypatch.setattr(
        storage, "upload_llms_txt",
        lambda content, key: f"https://bucket.s3.us-east-2.amazonaws.com/{key}",
    )


@pytest.fixture
def anyio_backend():
    # anyio's pytest plugin parametrizes over backends; we only run asyncio,
    # so pin it — otherwise every async test also runs under trio.
    return "asyncio"

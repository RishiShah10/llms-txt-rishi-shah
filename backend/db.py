import hashlib
import os

import psycopg
from psycopg.types.json import Json

_SCHEMA = """
CREATE TABLE IF NOT EXISTS generations (
    site_url      TEXT PRIMARY KEY,
    scope_prefix  TEXT,
    content_hash  TEXT NOT NULL,
    page_count    INT  NOT NULL,
    warnings      JSONB NOT NULL DEFAULT '[]',
    object_key    TEXT,
    public_url    TEXT,
    crawl         BOOLEAN NOT NULL DEFAULT true,
    max_pages     INT,
    enhance       BOOLEAN NOT NULL DEFAULT false,
    bypass        BOOLEAN NOT NULL DEFAULT false,
    honor_robots  BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# CREATE TABLE IF NOT EXISTS won't touch an existing table, so columns added
# after first deploy need their own idempotent migration.
_MIGRATIONS = (
    "ALTER TABLE generations ADD COLUMN IF NOT EXISTS honor_robots BOOLEAN NOT NULL DEFAULT true;",
    "ALTER TABLE generations ADD COLUMN IF NOT EXISTS llms_txt TEXT;",
    "ALTER TABLE generations ADD COLUMN IF NOT EXISTS object_key TEXT;",
    "ALTER TABLE generations ADD COLUMN IF NOT EXISTS public_url TEXT;",
    "ALTER TABLE generations DROP COLUMN IF EXISTS llms_txt;",
    "ALTER TABLE generations ADD COLUMN IF NOT EXISTS auto_update BOOLEAN NOT NULL DEFAULT false;",
    "ALTER TABLE generations ADD COLUMN IF NOT EXISTS recrawl_interval_days INT;",
    "ALTER TABLE generations ADD COLUMN IF NOT EXISTS next_check_at TIMESTAMPTZ;",
    "ALTER TABLE generations ADD COLUMN IF NOT EXISTS sitemap_newest_lastmod TIMESTAMPTZ;",
)

_LOAD_COLUMNS = (
    "content_hash", "object_key", "public_url", "scope_prefix",
    "crawl", "max_pages", "enhance", "bypass", "honor_robots",
    "sitemap_newest_lastmod",
)


# psycopg blocks forever on a TCP connect by default. These run in a threadpool
# worker; a cancelled call orphans that worker, so bound how long an unreachable
# DB can tie up a pool slot. All persistence here is best-effort -- fail fast.
CONNECT_TIMEOUT_SECONDS = 10


def _dsn() -> str:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    return dsn


def _connect():
    # keyword connect_timeout wins over any in the DSN, so the bound always holds.
    return psycopg.connect(_dsn(), connect_timeout=CONNECT_TIMEOUT_SECONDS)


def init_db() -> None:
    with _connect() as conn:
        conn.execute(_SCHEMA)
        for migration in _MIGRATIONS:
            conn.execute(migration)


def site_hash(pages: list[dict]) -> str:
    # Sorted so page order doesn't affect the hash; a differing hash means the
    # generated file would differ. This is the change-detection signal.
    parts = sorted(
        f"{page['url']}\x00{page.get('title', '')}\x00{page.get('description') or ''}"
        for page in pages
    )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def save_generation(record: dict) -> None:
    # Upsert on site_url: re-generating overwrites in place (no version history),
    # preserving created_at. The file lives in S3; we keep only its key + url.
    params = {**record, "warnings": Json(record.get("warnings", []))}
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO generations
                (site_url, scope_prefix, content_hash, page_count, warnings,
                 object_key, public_url, crawl, max_pages, enhance, bypass, honor_robots, updated_at)
            VALUES (%(site_url)s, %(scope_prefix)s, %(content_hash)s, %(page_count)s,
                    %(warnings)s, %(object_key)s, %(public_url)s, %(crawl)s, %(max_pages)s,
                    %(enhance)s, %(bypass)s, %(honor_robots)s, now())
            ON CONFLICT (site_url) DO UPDATE SET
                scope_prefix = EXCLUDED.scope_prefix,
                content_hash = EXCLUDED.content_hash,
                page_count   = EXCLUDED.page_count,
                warnings     = EXCLUDED.warnings,
                object_key   = EXCLUDED.object_key,
                public_url   = EXCLUDED.public_url,
                crawl        = EXCLUDED.crawl,
                max_pages    = EXCLUDED.max_pages,
                enhance      = EXCLUDED.enhance,
                bypass       = EXCLUDED.bypass,
                honor_robots = EXCLUDED.honor_robots,
                updated_at   = now();
            """,
            params,
        )


def enroll_auto_update(site_url: str, interval_days: int) -> None:
    # Separate from save_generation (whose upsert doesn't list these columns) so a
    # re-generate never changes enrollment. next_check_at snaps to a UTC midnight to
    # line up with the nightly sweep, not to 24h after the click.
    with _connect() as conn:
        conn.execute(
            """
            UPDATE generations
            SET auto_update = true,
                recrawl_interval_days = %s,
                next_check_at = date_trunc('day', now()) + make_interval(days => %s)
            WHERE site_url = %s;
            """,
            (interval_days, interval_days, site_url),
        )


def load_due_sites() -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT site_url FROM generations WHERE auto_update AND next_check_at <= now()"
        ).fetchall()
    return [row[0] for row in rows]


def schedule_next_check(site_url: str) -> None:
    # Bumped after EVERY attempt (success or fail) so a perpetually-erroring site
    # doesn't stay due and hot-loop the sweep. Snapped to a midnight boundary.
    with _connect() as conn:
        conn.execute(
            """
            UPDATE generations
            SET next_check_at = date_trunc('day', now())
                + make_interval(days => COALESCE(recrawl_interval_days, 1))
            WHERE site_url = %s;
            """,
            (site_url,),
        )


def record_sitemap_lastmod(site_url: str, lastmod) -> None:
    # Written after a full refresh so the next sweep's freshness gate can skip
    # the crawl when the sitemap's newest <lastmod> hasn't advanced.
    with _connect() as conn:
        conn.execute(
            "UPDATE generations SET sitemap_newest_lastmod = %s WHERE site_url = %s;",
            (lastmod, site_url),
        )


def load_generation(site_url: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_LOAD_COLUMNS)} FROM generations WHERE site_url = %s",
            (site_url,),
        ).fetchone()
    return dict(zip(_LOAD_COLUMNS, row)) if row is not None else None

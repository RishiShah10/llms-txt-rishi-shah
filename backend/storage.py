import os
import re

import boto3
from botocore.config import Config

# Bucket/region come from the ECS task def env in prod; module-level so tests
# can monkeypatch them.
_BUCKET = os.getenv("S3_BUCKET")
_REGION = os.getenv("S3_REGION", "us-east-2")

_UNSAFE = re.compile(r"[^A-Za-z0-9._/-]+")
_client = None

# boto3's default (60s read x 5 retries) can tie up a threadpool worker for minutes
# on a slow S3 call; a cancelled call orphans that worker. Bound it -- upload is
# best-effort anyway. Fail fast.
_TIMEOUTS = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2})


def _s3():
    # Lazy singleton; boto3 resolves credentials via the default chain (the ECS
    # task role in prod). Import-time construction would need creds even in tests.
    global _client
    if _client is None:
        _client = boto3.client("s3", region_name=_REGION, config=_TIMEOUTS)
    return _client


def object_key_for(site_url: str) -> str:
    # Readable, structure-preserving key: drop scheme + surrounding slashes,
    # replace unsafe runs with '-', keep path slashes so distinct URLs don't collide.
    s = site_url.split("://", 1)[-1].strip("/")
    s = _UNSAFE.sub("-", s)
    return f"{s}.txt" if s else "index.txt"


def public_url_for(key: str) -> str:
    return f"https://{_BUCKET}.s3.{_REGION}.amazonaws.com/{key}"


def upload_llms_txt(content: str, key: str) -> str:
    _s3().put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )
    return public_url_for(key)

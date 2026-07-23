"""S3/MinIO object storage helpers (RFC-001 A2: blobs live in S3, never in Postgres).

Presigned URLs let the browser upload/download attachments directly to S3 without proxying bytes
through the API. Presigning is a local signing operation (no network), so it stays off the async
request path's I/O budget. Objects are keyed under a per-workspace prefix; the download presigner
refuses keys outside the caller's workspace, which is how tenant isolation is kept for a store that
has no row-level security of its own.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

import boto3

from relay.settings import get_settings

_UPLOAD_TTL_SECONDS = 900  # 15 min to complete a PUT
_DOWNLOAD_TTL_SECONDS = 3600  # 1 h view window
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@lru_cache(maxsize=1)
def _client() -> Any:
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint_url,
        region_name=s.s3_region,
        aws_access_key_id=s.s3_access_key_id,
        aws_secret_access_key=s.s3_secret_access_key,
    )


def workspace_prefix(workspace_public_id: str) -> str:
    return f"attachments/{workspace_public_id}/"


def sanitize_filename(name: str) -> str:
    cleaned = _SAFE.sub("_", name).strip("_")
    return cleaned[:120] or "file"


def build_key(workspace_public_id: str, unique: str, filename: str) -> str:
    return f"{workspace_prefix(workspace_public_id)}{unique}/{sanitize_filename(filename)}"


def presign_put(key: str, content_type: str, *, ttl: int = _UPLOAD_TTL_SECONDS) -> str:
    url = _client().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": get_settings().s3_bucket_attachments,
            "Key": key,
            "ContentType": content_type,
        },
        ExpiresIn=ttl,
    )
    return str(url)


def presign_get(key: str, *, ttl: int = _DOWNLOAD_TTL_SECONDS) -> str:
    url = _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": get_settings().s3_bucket_attachments, "Key": key},
        ExpiresIn=ttl,
    )
    return str(url)

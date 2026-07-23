"""Pydantic models for the `platform` module (uploads, webhooks — built out across phase 0)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PresignUploadIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=255)


class PresignUploadOut(BaseModel):
    """A presigned S3 PUT for a direct browser upload, plus the object key to store as the
    attachment ref (never the bytes — RFC-001 A2)."""

    key: str
    upload_url: str
    method: str = "PUT"


class DownloadUrlOut(BaseModel):
    url: str

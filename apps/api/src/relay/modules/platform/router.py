"""HTTP routes for the `platform` module. Mounted by relay.main under the versioned API."""

from __future__ import annotations

from fastapi import APIRouter, Query

from relay.core.deps import CurrentPrincipal

from . import schemas, service

router = APIRouter(tags=["platform"])


# --- Attachment uploads (presigned S3, RFC-001 A2 / §10) ----------------------


@router.post("/uploads/presign", response_model=schemas.PresignUploadOut)
async def presign_upload(
    req: schemas.PresignUploadIn, principal: CurrentPrincipal
) -> schemas.PresignUploadOut:
    """Get a presigned PUT so the browser uploads an attachment straight to S3 (no API proxy)."""
    return service.presign_attachment_upload(principal, req)


@router.get("/uploads/download-url", response_model=schemas.DownloadUrlOut)
async def download_url(
    principal: CurrentPrincipal,
    key: str = Query(..., description="S3 object key returned by /uploads/presign"),
) -> schemas.DownloadUrlOut:
    """Get a short-lived presigned GET for an attachment the caller's workspace owns."""
    return service.presign_attachment_download(principal, key)

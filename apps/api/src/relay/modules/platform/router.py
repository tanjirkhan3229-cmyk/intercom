"""HTTP routes for the `platform` module. Mounted by relay.main under the versioned API."""

from __future__ import annotations

from fastapi import APIRouter, Query

from relay.core.deps import ContactSession, CurrentPrincipal

from . import schemas, service

router = APIRouter(tags=["platform"])


# --- Attachment uploads (presigned S3, RFC-001 A2 / §10) ----------------------


@router.post("/uploads/presign", response_model=schemas.PresignUploadOut)
async def presign_upload(
    req: schemas.PresignUploadIn, principal: CurrentPrincipal
) -> schemas.PresignUploadOut:
    """Get a presigned PUT so the browser uploads an attachment straight to S3 (no API proxy)."""
    return service.presign_upload_for_workspace(principal.workspace_id, req)


@router.get("/uploads/download-url", response_model=schemas.DownloadUrlOut)
async def download_url(
    principal: CurrentPrincipal,
    key: str = Query(..., description="S3 object key returned by /uploads/presign"),
) -> schemas.DownloadUrlOut:
    """Get a short-lived presigned GET for an attachment the caller's workspace owns."""
    return service.presign_download_for_workspace(principal.workspace_id, key)


# --- Widget/mobile attachment uploads (contact-authenticated, P1.10) ----------
# The messenger widget + iOS/Android SDKs upload as an end-user (contact) session, so they need
# a contact-facing presign that scopes to the contact's workspace (agents use the routes above).


@router.post("/widget/uploads/presign", response_model=schemas.PresignUploadOut)
async def widget_presign_upload(
    req: schemas.PresignUploadIn, contact: ContactSession
) -> schemas.PresignUploadOut:
    """Presigned PUT so a widget/mobile contact uploads an attachment straight to S3."""
    return service.presign_upload_for_workspace(contact.workspace_id, req)


@router.get("/widget/uploads/download-url", response_model=schemas.DownloadUrlOut)
async def widget_download_url(
    contact: ContactSession,
    key: str = Query(..., description="S3 object key returned by /widget/uploads/presign"),
) -> schemas.DownloadUrlOut:
    """Short-lived presigned GET for an attachment in the contact's workspace."""
    return service.presign_download_for_workspace(contact.workspace_id, key)

"""Service interface for the `platform` module.

This is the ONLY surface other modules may import (plus `events`). Reaching into
another module's `models`/`router` is forbidden and enforced by import-linter
(see .importlinter). Modules otherwise communicate via domain events on the outbox.
"""

from __future__ import annotations

from relay.core import storage
from relay.core.errors import PermissionDeniedError
from relay.core.ids import IdPrefix, encode_public_id, uuid7_str
from relay.core.principal import Principal

from . import schemas


def presign_attachment_upload(
    principal: Principal, req: schemas.PresignUploadIn
) -> schemas.PresignUploadOut:
    """Mint a presigned PUT for an attachment, keyed under the caller's workspace prefix."""
    ws = encode_public_id(IdPrefix.WORKSPACE, principal.workspace_id)
    key = storage.build_key(ws, uuid7_str(), req.filename)
    url = storage.presign_put(key, req.content_type)
    return schemas.PresignUploadOut(key=key, upload_url=url)


def presign_attachment_download(principal: Principal, key: str) -> schemas.DownloadUrlOut:
    """Mint a presigned GET — but only for objects under the caller's workspace prefix. S3 has no
    RLS, so this prefix check is the tenant-isolation boundary (RFC-001 §10)."""
    ws = encode_public_id(IdPrefix.WORKSPACE, principal.workspace_id)
    if not key.startswith(storage.workspace_prefix(ws)):
        raise PermissionDeniedError("attachment does not belong to this workspace")
    return schemas.DownloadUrlOut(url=storage.presign_get(key))

"""HTTP routes for the ``crm`` module (RFC-002 §5.4). Mounted by relay.main under ``/v0``.

Auth/tenancy come from the shared kernel dependencies (``relay.core.deps``); RBAC is enforced
in the service layer through the ``authorize`` choke point (RFC-001 §10). All list endpoints
are keyset-paginated (``relay.core.pagination.Page``).
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Response, status

from relay.core.deps import CurrentPrincipal, SessionDep
from relay.core.pagination import Page

from . import schemas, service

router = APIRouter(tags=["crm"])


# --- Contacts -----------------------------------------------------------------


@router.post("/contacts/identify", response_model=schemas.ContactOut)
async def identify(
    req: schemas.ContactIdentify, principal: CurrentPrincipal, session: SessionDep
) -> schemas.ContactOut:
    return await service.identify(session, principal, req)


@router.post("/contacts", response_model=schemas.ContactOut, status_code=201)
async def create_contact(
    req: schemas.ContactCreate, principal: CurrentPrincipal, session: SessionDep
) -> schemas.ContactOut:
    return await service.create_contact(session, principal, req)


@router.get("/contacts", response_model=Page[schemas.ContactOut])
async def list_contacts(
    _principal: CurrentPrincipal,
    session: SessionDep,
    q: str | None = Query(default=None, description="trigram typeahead on name"),
    cursor: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
) -> Page[schemas.ContactOut]:
    return await service.list_contacts(session, q=q, cursor=cursor, limit=limit)


@router.get("/contacts/{contact_id}", response_model=schemas.ContactOut)
async def get_contact(
    contact_id: str, _principal: CurrentPrincipal, session: SessionDep
) -> schemas.ContactOut:
    return await service.get_contact(session, contact_id)


@router.get("/contacts/{contact_id}/events", response_model=list[schemas.EventOut])
async def list_contact_events(
    contact_id: str,
    _principal: CurrentPrincipal,
    session: SessionDep,
    limit: int | None = Query(default=None, ge=1, le=200),
) -> list[schemas.EventOut]:
    """Recent tracked events for a contact (inbox side panel activity feed)."""
    return await service.list_recent_events(session, contact_id, limit=limit)


@router.patch("/contacts/{contact_id}", response_model=schemas.ContactOut)
async def update_contact(
    contact_id: str,
    req: schemas.ContactUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.ContactOut:
    return await service.update_contact(session, principal, contact_id, req)


@router.delete("/contacts/{contact_id}", status_code=204)
async def delete_contact(
    contact_id: str, principal: CurrentPrincipal, session: SessionDep
) -> Response:
    await service.delete_contact(session, principal, contact_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/contacts/{contact_id}/companies", response_model=list[schemas.CompanyOut])
async def list_contact_companies(
    contact_id: str, _principal: CurrentPrincipal, session: SessionDep
) -> list[schemas.CompanyOut]:
    return await service.list_contact_companies(session, contact_id)


@router.post("/contacts/{contact_id}/companies", status_code=204)
async def link_company(
    contact_id: str,
    req: schemas.ContactCompanyLink,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> Response:
    await service.link_company(session, principal, contact_id, req.company_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/contacts/{contact_id}/companies/{company_id}", status_code=204)
async def unlink_company(
    contact_id: str, company_id: str, principal: CurrentPrincipal, session: SessionDep
) -> Response:
    await service.unlink_company(session, principal, contact_id, company_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Companies ----------------------------------------------------------------


@router.post("/companies", response_model=schemas.CompanyOut, status_code=201)
async def create_company(
    req: schemas.CompanyCreate, principal: CurrentPrincipal, session: SessionDep
) -> schemas.CompanyOut:
    return await service.create_company(session, principal, req)


@router.get("/companies", response_model=Page[schemas.CompanyOut])
async def list_companies(
    _principal: CurrentPrincipal,
    session: SessionDep,
    cursor: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
) -> Page[schemas.CompanyOut]:
    return await service.list_companies(session, cursor=cursor, limit=limit)


@router.get("/companies/{company_id}", response_model=schemas.CompanyOut)
async def get_company(
    company_id: str, _principal: CurrentPrincipal, session: SessionDep
) -> schemas.CompanyOut:
    return await service.get_company(session, company_id)


@router.patch("/companies/{company_id}", response_model=schemas.CompanyOut)
async def update_company(
    company_id: str,
    req: schemas.CompanyUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.CompanyOut:
    return await service.update_company(session, principal, company_id, req)


@router.delete("/companies/{company_id}", status_code=204)
async def delete_company(
    company_id: str, principal: CurrentPrincipal, session: SessionDep
) -> Response:
    await service.delete_company(session, principal, company_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Attribute definitions ----------------------------------------------------


@router.post(
    "/attribute-definitions",
    response_model=schemas.AttributeDefinitionOut,
    status_code=201,
)
async def create_attribute_definition(
    req: schemas.AttributeDefinitionCreate, principal: CurrentPrincipal, session: SessionDep
) -> schemas.AttributeDefinitionOut:
    return await service.create_attribute_definition(session, principal, req)


@router.get("/attribute-definitions", response_model=list[schemas.AttributeDefinitionOut])
async def list_attribute_definitions(
    _principal: CurrentPrincipal,
    session: SessionDep,
    entity: str | None = Query(default=None, pattern="^(contact|company)$"),
) -> list[schemas.AttributeDefinitionOut]:
    return await service.list_attribute_definitions(session, entity=entity)


@router.delete("/attribute-definitions/{definition_id}", status_code=204)
async def delete_attribute_definition(
    definition_id: str, principal: CurrentPrincipal, session: SessionDep
) -> Response:
    await service.delete_attribute_definition(session, principal, definition_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Events -------------------------------------------------------------------


@router.post("/events/track", response_model=schemas.TrackResponse, status_code=202)
async def track_events(
    req: schemas.TrackRequest, principal: CurrentPrincipal, session: SessionDep
) -> schemas.TrackResponse:
    accepted = await service.track_events(session, principal, req)
    return schemas.TrackResponse(accepted=accepted)

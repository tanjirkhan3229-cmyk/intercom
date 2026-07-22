"""Service layer for the ``crm`` module — the cross-module interface (RFC-002 §5.4).

This is the ONLY surface other modules may import (plus ``events``). Reaching into another
module's ``models``/``router`` is forbidden and enforced by import-linter.

Highlights:
- ``identify`` — the W2 idempotent upsert. Keyed by ``external_id`` (preferred) or ``email``
  (for ``kind='user'``) via ``INSERT … ON CONFLICT`` on the partial unique indexes, so two
  calls with the same key converge on one contact even under a race. Merge rules are on the
  function.
- ``validate_custom`` — every write to a ``custom`` JSONB is checked against
  ``attribute_definitions`` (the swamp guard, RFC-002 §12): unknown keys and type mismatches
  are rejected with 422.
- ``track_events`` — buffers to a per-workspace Redis list; the analytics drain (tasks.py)
  lands them via COPY into the partitioned ``events`` table.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core.errors import ConflictError, NotFoundError, ValidationError
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id
from relay.core.pagination import Page, clamp_limit
from relay.core.principal import Principal
from relay.core.rbac import Role, authorize
from relay.core.redis import get_redis

from . import schemas
from .models import AttributeDefinition, Company, Contact, ContactCompany

# Redis keys for the event firehose buffer (RFC-002 §5.4 W3).
EVENTS_BUFFER_PREFIX = "events:buffer:"
EVENTS_BUFFER_WORKSPACES = "events:buffer:workspaces"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def events_buffer_key(workspace_id: uuid.UUID) -> str:
    return f"{EVENTS_BUFFER_PREFIX}{workspace_id}"


# --- DTO builders -------------------------------------------------------------


def contact_out(c: Contact) -> schemas.ContactOut:
    return schemas.ContactOut(
        id=encode_public_id(IdPrefix.CONTACT, c.id),
        kind=c.kind,
        external_id=c.external_id,
        email=c.email,
        phone=c.phone,
        name=c.name,
        custom=c.custom,
        last_seen_at=c.last_seen_at,
        created_at=c.created_at,
    )


def company_out(c: Company) -> schemas.CompanyOut:
    return schemas.CompanyOut(
        id=encode_public_id(IdPrefix.COMPANY, c.id),
        external_id=c.external_id,
        name=c.name,
        domain=c.domain,
        custom=c.custom,
        created_at=c.created_at,
    )


def attribute_definition_out(a: AttributeDefinition) -> schemas.AttributeDefinitionOut:
    return schemas.AttributeDefinitionOut(
        id=encode_public_id(IdPrefix.ATTRIBUTE, a.id),
        entity=a.entity,
        name=a.name,
        data_type=a.data_type,
        label=a.label,
        created_at=a.created_at,
    )


def _decode_or_404(prefix: str, public_id: str, what: str) -> uuid.UUID:
    try:
        return decode_public_id(prefix, public_id)
    except ValueError as exc:
        raise NotFoundError(f"{what} not found") from exc


# --- Custom-attribute validation (the swamp guard, RFC-002 §12) ---------------


def _type_ok(data_type: str, value: Any) -> bool:
    if value is None:
        return True  # null clears an attribute; always allowed
    if data_type == "string":
        return isinstance(value, str)
    if data_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if data_type == "boolean":
        return isinstance(value, bool)
    if data_type == "list":
        return isinstance(value, list)
    if data_type == "date":
        if not isinstance(value, str):
            return False
        try:
            dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        except ValueError:
            try:
                dt.date.fromisoformat(value)
                return True
            except ValueError:
                return False
    return False


async def _load_attr_defs(session: AsyncSession, entity: str) -> dict[str, str]:
    rows = await session.execute(
        select(AttributeDefinition.name, AttributeDefinition.data_type).where(
            AttributeDefinition.entity == entity
        )
    )
    return dict(rows.tuples().all())


async def validate_custom(session: AsyncSession, entity: str, custom: dict[str, Any]) -> None:
    """Reject unknown keys and type mismatches (422). ``attribute_definitions`` is the only
    write path for ``custom`` — undefined keys are refused so the JSONB never becomes a swamp.
    """
    if not custom:
        return
    defs = await _load_attr_defs(session, entity)
    unknown = [k for k in custom if k not in defs]
    if unknown:
        raise ValidationError(
            "unknown custom attribute(s); define them via attribute-definitions first",
            details={"unknown": sorted(unknown), "entity": entity},
        )
    mismatched = {k: defs[k] for k, v in custom.items() if not _type_ok(defs[k], v)}
    if mismatched:
        raise ValidationError(
            "custom attribute type mismatch",
            details={"expected": mismatched, "entity": entity},
        )


# --- Contacts: identify (W2) --------------------------------------------------


async def identify(
    session: AsyncSession, principal: Principal, req: schemas.ContactIdentify
) -> schemas.ContactOut:
    """Idempotent upsert of a contact (RFC-002 W2).

    Merge rules (documented contract):
    - **Identity key:** ``external_id`` when present (upsert on ``contacts_ext``), else
      ``email`` for a ``user`` (upsert on ``contacts_email_user``). Two calls with the same
      key resolve to exactly one row, even concurrently (``ON CONFLICT`` on the partial
      unique index).
    - **On insert:** ``kind`` defaults to ``user`` (identify = a known user).
    - **On conflict:** provided scalar fields overwrite only when non-null
      (``COALESCE(EXCLUDED.x, existing.x)``); ``last_seen_at`` advances monotonically
      (``GREATEST``); ``custom`` is shallow-merged (``existing || provided`` — provided wins
      per top-level key); ``kind`` changes only when explicitly provided.
    - **Collisions** (an ``email`` already owned by a *different* contact than the matched
      ``external_id``) are left intact and reconciled by the explicit merge job (RFC-002
      §5.4) — out of scope for P0.2.
    """
    authorize(principal, min_role=Role.AGENT)
    await validate_custom(session, "contact", req.custom)

    insert_kind = req.kind or "user"
    values: dict[str, Any] = {
        "workspace_id": principal.workspace_id,
        "kind": insert_kind,
        "external_id": req.external_id,
        "email": req.email,
        "phone": req.phone,
        "name": req.name,
        "custom": req.custom or {},
        "last_seen_at": req.last_seen_at,
    }
    stmt = pg_insert(Contact).values(**values)
    excluded = stmt.excluded

    set_: dict[str, Any] = {
        "email": func.coalesce(excluded.email, Contact.email),
        "phone": func.coalesce(excluded.phone, Contact.phone),
        "name": func.coalesce(excluded.name, Contact.name),
        "last_seen_at": func.greatest(Contact.last_seen_at, excluded.last_seen_at),
        "custom": Contact.custom.op("||")(excluded.custom),
    }
    if req.kind is not None:
        set_["kind"] = excluded.kind

    if req.external_id:
        stmt = stmt.on_conflict_do_update(
            index_elements=[Contact.workspace_id, Contact.external_id],
            index_where=sa.and_(Contact.external_id.isnot(None), Contact.deleted_at.is_(None)),
            set_=set_,
        )
    else:
        # email-only path forces kind='user' to match the partial index predicate.
        stmt = stmt.values(kind="user").on_conflict_do_update(
            index_elements=[Contact.workspace_id, Contact.email],
            index_where=sa.and_(
                Contact.kind == "user",
                Contact.email.isnot(None),
                Contact.deleted_at.is_(None),
            ),
            set_=set_,
        )

    contact = (await session.execute(stmt.returning(Contact))).scalar_one()
    await session.flush()
    return contact_out(contact)


# --- Contacts: CRUD -----------------------------------------------------------


async def create_contact(
    session: AsyncSession, principal: Principal, req: schemas.ContactCreate
) -> schemas.ContactOut:
    authorize(principal, min_role=Role.AGENT)
    await validate_custom(session, "contact", req.custom)
    contact = Contact(
        workspace_id=principal.workspace_id,
        kind=req.kind,
        external_id=req.external_id,
        email=req.email,
        phone=req.phone,
        name=req.name,
        custom=req.custom or {},
    )
    session.add(contact)
    try:
        await session.flush()
    except sa.exc.IntegrityError as exc:
        raise ConflictError("a contact with this external_id or email already exists") from exc
    return contact_out(contact)


async def _get_contact(session: AsyncSession, contact_id: uuid.UUID) -> Contact:
    contact = await session.get(Contact, contact_id)
    if contact is None or contact.deleted_at is not None:
        raise NotFoundError("contact not found")
    return contact


async def get_contact(session: AsyncSession, public_id: str) -> schemas.ContactOut:
    cid = _decode_or_404(IdPrefix.CONTACT, public_id, "contact")
    return contact_out(await _get_contact(session, cid))


async def list_contacts(
    session: AsyncSession,
    *,
    q: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> Page[schemas.ContactOut]:
    """List contacts. ``q`` runs trigram typeahead on ``name`` (uses ``contacts_name_trgm``);
    otherwise keyset pagination by ``id`` DESC (uuid7 → newest first). Never OFFSET."""
    n = clamp_limit(limit)
    base = select(Contact).where(Contact.deleted_at.is_(None))

    if q:
        # Trigram typeahead (R8). ILIKE '%q%' is served by the GIN gin_trgm_ops index.
        stmt = base.where(Contact.name.ilike(f"%{q}%")).order_by(Contact.name).limit(n)
        contacts = (await session.scalars(stmt)).all()
        return Page(items=[contact_out(c) for c in contacts], next_cursor=None)

    if cursor:
        cur = _decode_or_404(IdPrefix.CONTACT, cursor, "cursor")
        base = base.where(Contact.id < cur)
    contacts = (await session.scalars(base.order_by(Contact.id.desc()).limit(n + 1))).all()
    next_cursor = None
    if len(contacts) > n:
        contacts = list(contacts[:n])
        next_cursor = encode_public_id(IdPrefix.CONTACT, contacts[-1].id)
    return Page(items=[contact_out(c) for c in contacts], next_cursor=next_cursor)


async def update_contact(
    session: AsyncSession, principal: Principal, public_id: str, req: schemas.ContactUpdate
) -> schemas.ContactOut:
    authorize(principal, min_role=Role.AGENT)
    cid = _decode_or_404(IdPrefix.CONTACT, public_id, "contact")
    contact = await _get_contact(session, cid)
    if req.custom is not None:
        await validate_custom(session, "contact", req.custom)
        contact.custom = {**contact.custom, **req.custom}
    for field in ("email", "phone", "name", "kind", "last_seen_at"):
        val = getattr(req, field)
        if val is not None:
            setattr(contact, field, val)
    try:
        await session.flush()
    except sa.exc.IntegrityError as exc:
        raise ConflictError("a contact with this external_id or email already exists") from exc
    return contact_out(contact)


async def delete_contact(session: AsyncSession, principal: Principal, public_id: str) -> None:
    """Soft delete (RFC-002 §5.4: contacts use ``deleted_at``). Frees the partial-unique keys."""
    authorize(principal, min_role=Role.AGENT)
    cid = _decode_or_404(IdPrefix.CONTACT, public_id, "contact")
    contact = await _get_contact(session, cid)
    contact.deleted_at = _now()
    await session.flush()


# --- Companies: CRUD ----------------------------------------------------------


async def create_company(
    session: AsyncSession, principal: Principal, req: schemas.CompanyCreate
) -> schemas.CompanyOut:
    authorize(principal, min_role=Role.AGENT)
    await validate_custom(session, "company", req.custom)
    company = Company(
        workspace_id=principal.workspace_id,
        external_id=req.external_id,
        name=req.name,
        domain=req.domain,
        custom=req.custom or {},
    )
    session.add(company)
    await session.flush()
    return company_out(company)


async def _get_company(session: AsyncSession, company_id: uuid.UUID) -> Company:
    company = await session.get(Company, company_id)
    if company is None:
        raise NotFoundError("company not found")
    return company


async def get_company(session: AsyncSession, public_id: str) -> schemas.CompanyOut:
    cid = _decode_or_404(IdPrefix.COMPANY, public_id, "company")
    return company_out(await _get_company(session, cid))


async def list_companies(
    session: AsyncSession, *, cursor: str | None = None, limit: int | None = None
) -> Page[schemas.CompanyOut]:
    n = clamp_limit(limit)
    stmt = select(Company)
    if cursor:
        cur = _decode_or_404(IdPrefix.COMPANY, cursor, "cursor")
        stmt = stmt.where(Company.id < cur)
    companies = (await session.scalars(stmt.order_by(Company.id.desc()).limit(n + 1))).all()
    next_cursor = None
    if len(companies) > n:
        companies = list(companies[:n])
        next_cursor = encode_public_id(IdPrefix.COMPANY, companies[-1].id)
    return Page(items=[company_out(c) for c in companies], next_cursor=next_cursor)


async def update_company(
    session: AsyncSession, principal: Principal, public_id: str, req: schemas.CompanyUpdate
) -> schemas.CompanyOut:
    authorize(principal, min_role=Role.AGENT)
    cid = _decode_or_404(IdPrefix.COMPANY, public_id, "company")
    company = await _get_company(session, cid)
    if req.custom is not None:
        await validate_custom(session, "company", req.custom)
        company.custom = {**company.custom, **req.custom}
    for field in ("name", "domain"):
        val = getattr(req, field)
        if val is not None:
            setattr(company, field, val)
    await session.flush()
    return company_out(company)


async def delete_company(session: AsyncSession, principal: Principal, public_id: str) -> None:
    authorize(principal, min_role=Role.AGENT)
    cid = _decode_or_404(IdPrefix.COMPANY, public_id, "company")
    company = await _get_company(session, cid)
    await session.delete(company)
    await session.flush()


# --- Contact ↔ company links --------------------------------------------------


async def link_company(
    session: AsyncSession, principal: Principal, contact_public_id: str, company_public_id: str
) -> None:
    authorize(principal, min_role=Role.AGENT)
    contact = await _get_contact(
        session, _decode_or_404(IdPrefix.CONTACT, contact_public_id, "contact")
    )
    company = await _get_company(
        session, _decode_or_404(IdPrefix.COMPANY, company_public_id, "company")
    )
    stmt = (
        pg_insert(ContactCompany)
        .values(workspace_id=principal.workspace_id, contact_id=contact.id, company_id=company.id)
        .on_conflict_do_nothing(
            index_elements=[
                ContactCompany.workspace_id,
                ContactCompany.contact_id,
                ContactCompany.company_id,
            ]
        )
    )
    await session.execute(stmt)
    await session.flush()


async def unlink_company(
    session: AsyncSession, principal: Principal, contact_public_id: str, company_public_id: str
) -> None:
    authorize(principal, min_role=Role.AGENT)
    contact_id = _decode_or_404(IdPrefix.CONTACT, contact_public_id, "contact")
    company_id = _decode_or_404(IdPrefix.COMPANY, company_public_id, "company")
    await session.execute(
        sa.delete(ContactCompany).where(
            ContactCompany.contact_id == contact_id,
            ContactCompany.company_id == company_id,
        )
    )
    await session.flush()


async def list_contact_companies(
    session: AsyncSession, contact_public_id: str
) -> list[schemas.CompanyOut]:
    contact_id = _decode_or_404(IdPrefix.CONTACT, contact_public_id, "contact")
    stmt = (
        select(Company)
        .join(ContactCompany, ContactCompany.company_id == Company.id)
        .where(ContactCompany.contact_id == contact_id)
        .order_by(Company.name)
    )
    return [company_out(c) for c in (await session.scalars(stmt)).all()]


# --- Attribute definitions ----------------------------------------------------


async def create_attribute_definition(
    session: AsyncSession, principal: Principal, req: schemas.AttributeDefinitionCreate
) -> schemas.AttributeDefinitionOut:
    authorize(principal, min_role=Role.ADMIN)
    definition = AttributeDefinition(
        workspace_id=principal.workspace_id,
        entity=req.entity,
        name=req.name,
        data_type=req.data_type,
        label=req.label,
    )
    session.add(definition)
    try:
        await session.flush()
    except sa.exc.IntegrityError as exc:
        raise ConflictError("an attribute with this name already exists for this entity") from exc
    return attribute_definition_out(definition)


async def list_attribute_definitions(
    session: AsyncSession, *, entity: str | None = None
) -> list[schemas.AttributeDefinitionOut]:
    stmt = select(AttributeDefinition)
    if entity:
        stmt = stmt.where(AttributeDefinition.entity == entity)
    stmt = stmt.order_by(AttributeDefinition.entity, AttributeDefinition.name)
    return [attribute_definition_out(a) for a in (await session.scalars(stmt)).all()]


async def delete_attribute_definition(
    session: AsyncSession, principal: Principal, public_id: str
) -> None:
    authorize(principal, min_role=Role.ADMIN)
    aid = _decode_or_404(IdPrefix.ATTRIBUTE, public_id, "attribute definition")
    definition = await session.get(AttributeDefinition, aid)
    if definition is None:
        raise NotFoundError("attribute definition not found")
    await session.delete(definition)
    await session.flush()


# --- Events: track (W3) -------------------------------------------------------


async def track_events(
    session: AsyncSession, principal: Principal, req: schemas.TrackRequest
) -> int:
    """Validate + buffer a batch of events to Redis (RFC-002 W3).

    Contact references are decoded and their existence verified under RLS (one query), then
    each event is JSON-serialized with its ``workspace_id`` stamped from the *authenticated*
    principal (never the client), so the analytics drain can never cross tenants. The
    partitioned ``events`` table is written by the ``analytics`` drain (tasks.py).
    """
    authorize(principal, min_role=Role.AGENT)
    workspace_id = principal.workspace_id
    contact_uuids: dict[str, uuid.UUID] = {}
    for e in req.events:
        contact_uuids[e.contact_id] = _decode_or_404(IdPrefix.CONTACT, e.contact_id, "contact")

    ids: set[uuid.UUID] = set(contact_uuids.values())
    existing = set(
        (
            await session.scalars(
                select(Contact.id).where(Contact.id.in_(ids), Contact.deleted_at.is_(None))
            )
        ).all()
    )
    missing = ids - existing
    if missing:
        raise ValidationError(
            "one or more events reference an unknown contact",
            details={"unknown_count": len(missing)},
        )

    now = _now()
    payloads: list[str] = [
        json.dumps(
            {
                "workspace_id": str(workspace_id),
                "contact_id": str(contact_uuids[e.contact_id]),
                "name": e.name,
                "properties": e.properties,
                "created_at": (e.created_at or now).isoformat(),
            }
        )
        for e in req.events
    ]

    redis = get_redis()
    pipe = redis.pipeline()
    pipe.rpush(events_buffer_key(workspace_id), *payloads)
    pipe.sadd(EVENTS_BUFFER_WORKSPACES, str(workspace_id))
    await pipe.execute()
    return len(payloads)

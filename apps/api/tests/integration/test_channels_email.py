"""Email channel integration tests (P0.7 acceptance, RFC-001 §6.6/§9, RFC-002 §5.6).

Hermetic: outbound goes through the in-process ``FakeSender`` (``email_transport='memory'``),
inbound raw MIME is injected via the ``fetch`` seam, and SNS verification is disabled (its verifier
has its own unit test). Covers the acceptance bar:
- round-trip send → reply → correct threading;
- duplicate SNS delivery ⇒ no duplicate part;
- bounce ⇒ suppression ⇒ next send blocked with a clear error;
- 50 MB attachment rejected politely (outbound raise / inbound omit + note);
plus the adversarial-review must-haves: outbound exactly-once (double-send chaos), reply-token +
In-Reply-To threading, sender-mismatch injection guard, dispatcher filtering, cross-tenant RLS.
"""

from __future__ import annotations

import asyncio
import email
import json
from email.message import EmailMessage
from uuid import uuid4

import httpx
import psycopg
import pytest
from sqlalchemy import func, select

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id
from relay.core.redis import get_redis, get_redis_sync
from relay.modules.channels import dispatch, sender, service
from relay.modules.channels.models import EmailDeliveryEvent, Suppression
from relay.modules.channels.models import EmailMessage as EmailLedger
from relay.modules.messaging.models import Conversation, ConversationPart
from relay.settings import get_settings

pytestmark = pytest.mark.integration

PASSWORD = "password123"
INBOUND_DOMAIN = "inbound.relay.dev"


@pytest.fixture(autouse=True)
def _email_env(monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "email_transport", "memory")
    monkeypatch.setattr(s, "sns_verify_signatures", False)
    monkeypatch.setattr(s, "email_inbound_domain", INBOUND_DOMAIN)
    sender.reset_sender()


async def _owner(client: httpx.AsyncClient, ws_name: str) -> tuple[str, str]:
    resp = await client.post(
        "/v0/auth/signup",
        json={
            "workspace_name": ws_name,
            "email": f"owner-{uuid4().hex}@example.com",
            "password": PASSWORD,
            "name": "Owner",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["access_token"], body["workspace"]["id"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _account(client: httpx.AsyncClient, tok: str, address: str) -> dict:
    r = await client.post(
        "/v0/channels/email/accounts", json={"address": address}, headers=_auth(tok)
    )
    assert r.status_code == 201, r.text
    return r.json()


def _inbound_raw(
    *,
    frm: str,
    to: str,
    subject: str,
    body: str,
    message_id: str | None = None,
    in_reply_to: str | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = to
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    return msg.as_bytes()


async def _ingest(
    raw: bytes, sns_id: str, key: str = "raw/mail.eml", recipients: list[str] | None = None
) -> str:
    return await service.ingest(
        sns_message_id=sns_id,
        s3_bucket="inbound",
        s3_key=key,
        recipients=recipients,
        fetch=lambda _b, _k: raw,
    )


async def _only_conversation(ws_id) -> Conversation:  # type: ignore[no-untyped-def]
    async with session_scope(ws_id) as s:
        convs = list((await s.scalars(select(Conversation))).all())
    assert len(convs) == 1, f"expected exactly one conversation, got {len(convs)}"
    return convs[0]


async def _part_count(ws_id, conversation_id) -> int:  # type: ignore[no-untyped-def]
    async with session_scope(ws_id) as s:
        return int(
            await s.scalar(
                select(func.count())
                .select_from(ConversationPart)
                .where(ConversationPart.conversation_id == conversation_id)
            )
            or 0
        )


async def _agent_reply(client: httpx.AsyncClient, tok: str, conv_public: str, body: str) -> str:
    r = await client.post(
        f"/v0/conversations/{conv_public}/reply", json={"body": body}, headers=_auth(tok)
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]  # part public id


# --- Round-trip: inbound → agent reply → threaded send ------------------------


async def test_round_trip_send_reply_threads(client: httpx.AsyncClient) -> None:
    tok, ws_pub = await _owner(client, "Acme")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    await _account(client, tok, "support@acme.io")

    raw = _inbound_raw(
        frm="Alice <alice@example.com>",
        to="support@acme.io",
        subject="Order late",
        body="Where is my order?",
        message_id="<in1@example.com>",
    )
    assert await _ingest(raw, f"sns-{uuid4().hex}") == "ingested"

    conv = await _only_conversation(ws_id)
    assert conv.channel == "email"
    assert conv.channel_account_id is not None

    conv_pub = encode_public_id(IdPrefix.CONVERSATION, conv.id)
    part_pub = await _agent_reply(client, tok, conv_pub, "On its way!")
    part_id = decode_public_id(IdPrefix.PART, part_pub)

    status = await service.send_email(workspace_id=ws_id, conversation_id=conv.id, part_id=part_id)
    assert status == "sent"

    sent = sender.fake_sender().sent
    assert len(sent) == 1
    assert sent[0].recipients == ["alice@example.com"]
    assert sent[0].sender == "support@acme.io"
    out = email.message_from_bytes(sent[0].raw)
    assert out["In-Reply-To"] == "<in1@example.com>"  # threaded onto the inbound
    assert out["Reply-To"].startswith("reply+")
    assert out["Subject"] == "Re: Order late"


# --- Duplicate SNS delivery ⇒ no duplicate part -------------------------------


async def test_duplicate_sns_no_duplicate_part(client: httpx.AsyncClient) -> None:
    tok, ws_pub = await _owner(client, "Dedupe")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    await _account(client, tok, "support@dedupe.io")
    raw = _inbound_raw(
        frm="bob@example.com",
        to="support@dedupe.io",
        subject="Hi",
        body="hello",
        message_id="<dup1@example.com>",
    )
    sns_id = f"sns-{uuid4().hex}"
    assert await _ingest(raw, sns_id) == "ingested"
    assert await _ingest(raw, sns_id) == "duplicate_sns"  # same SNS MessageId

    conv = await _only_conversation(ws_id)
    assert await _part_count(ws_id, conv.id) == 1


async def test_duplicate_message_id_different_sns(client: httpx.AsyncClient) -> None:
    tok, ws_pub = await _owner(client, "DedupeMid")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    await _account(client, tok, "support@dmid.io")
    raw = _inbound_raw(
        frm="bob@example.com",
        to="support@dmid.io",
        subject="Hi",
        body="hello",
        message_id="<samemid@example.com>",
    )
    assert await _ingest(raw, f"sns-{uuid4().hex}") == "ingested"
    # Different SNS envelope, same RFC-822 Message-ID → secondary dedupe catches it.
    assert await _ingest(raw, f"sns-{uuid4().hex}") == "duplicate_message_id"
    conv = await _only_conversation(ws_id)
    assert await _part_count(ws_id, conv.id) == 1


async def test_missing_message_id_still_dedupes(client: httpx.AsyncClient) -> None:
    tok, _ws_pub = await _owner(client, "NoMid")
    await _account(client, tok, "support@nomid.io")
    raw = _inbound_raw(frm="c@example.com", to="support@nomid.io", subject="x", body="y")
    assert await _ingest(raw, f"sns-{uuid4().hex}") == "ingested"
    # Same bytes, different SNS id, no Message-ID → synthesized id dedupes.
    assert await _ingest(raw, f"sns-{uuid4().hex}") == "duplicate_message_id"


# --- Threading: reply token + In-Reply-To -------------------------------------


async def _round_trip_to_outbound(client: httpx.AsyncClient, tok: str, ws_id, address: str):  # type: ignore[no-untyped-def]
    raw = _inbound_raw(
        frm="alice@example.com",
        to=address,
        subject="Help",
        body="q",
        message_id="<t-in@example.com>",
    )
    await _ingest(raw, f"sns-{uuid4().hex}")
    conv = await _only_conversation(ws_id)
    conv_pub = encode_public_id(IdPrefix.CONVERSATION, conv.id)
    part_pub = await _agent_reply(client, tok, conv_pub, "answer")
    await service.send_email(
        workspace_id=ws_id,
        conversation_id=conv.id,
        part_id=decode_public_id(IdPrefix.PART, part_pub),
    )
    out = email.message_from_bytes(sender.fake_sender().sent[-1].raw)
    return conv, out


async def test_reply_token_threads_into_same_conversation(client: httpx.AsyncClient) -> None:
    tok, ws_pub = await _owner(client, "TokThread")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    await _account(client, tok, "support@tok.io")
    conv, out = await _round_trip_to_outbound(client, tok, ws_id, "support@tok.io")

    reply_to = out["Reply-To"]
    inbound_reply = _inbound_raw(
        frm="alice@example.com",
        to=reply_to,
        subject="Re: Help",
        body="still stuck",
        message_id="<t-r1@example.com>",
    )
    assert await _ingest(inbound_reply, f"sns-{uuid4().hex}") == "ingested"

    assert (await _only_conversation(ws_id)).id == conv.id  # same thread
    assert await _part_count(ws_id, conv.id) == 3  # inbound + reply + inbound-reply


async def test_in_reply_to_threads_into_same_conversation(client: httpx.AsyncClient) -> None:
    tok, ws_pub = await _owner(client, "IrtThread")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    await _account(client, tok, "support@irt.io")
    conv, out = await _round_trip_to_outbound(client, tok, ws_id, "support@irt.io")

    inbound_reply = _inbound_raw(
        frm="alice@example.com",
        to="support@irt.io",
        subject="Re: Help",
        body="more",
        message_id="<irt-r1@example.com>",
        in_reply_to=out["Message-ID"],
    )
    assert await _ingest(inbound_reply, f"sns-{uuid4().hex}") == "ingested"
    assert (await _only_conversation(ws_id)).id == conv.id


async def test_sender_mismatch_starts_new_thread(client: httpx.AsyncClient) -> None:
    """A stranger replying to a leaked reply+ address must NOT be injected into the thread."""
    tok, ws_pub = await _owner(client, "Spoof")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    await _account(client, tok, "support@spoof.io")
    _conv, out = await _round_trip_to_outbound(client, tok, ws_id, "support@spoof.io")

    spoof = _inbound_raw(
        frm="attacker@evil.com",  # not the conversation's contact
        to=out["Reply-To"],
        subject="Re: Help",
        body="inject",
        message_id="<spoof1@evil.com>",
    )
    assert await _ingest(spoof, f"sns-{uuid4().hex}") == "ingested"
    async with session_scope(ws_id) as s:
        convs = list((await s.scalars(select(Conversation))).all())
    assert len(convs) == 2  # a new thread for the actual sender, not an injection


# --- Bounce → suppression → blocked send --------------------------------------


async def test_bounce_suppresses_and_blocks_next_send(client: httpx.AsyncClient) -> None:
    tok, ws_pub = await _owner(client, "Bounce")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    await _account(client, tok, "support@bounce.io")
    raw = _inbound_raw(
        frm="alice@example.com",
        to="support@bounce.io",
        subject="Hi",
        body="q",
        message_id="<b-in@example.com>",
    )
    await _ingest(raw, f"sns-{uuid4().hex}")
    conv = await _only_conversation(ws_id)

    bounce = {
        "notificationType": "Bounce",
        "mail": {"source": "support@bounce.io"},
        "bounce": {
            "bounceType": "Permanent",
            "bouncedRecipients": [{"emailAddress": "alice@example.com"}],
        },
    }
    assert await service.record_ses_event(message_json=json.dumps(bounce)) == "suppressed"
    async with session_scope(ws_id) as s:
        supp = list((await s.scalars(select(Suppression))).all())
    assert [x.email for x in supp] == ["alice@example.com"]

    conv_pub = encode_public_id(IdPrefix.CONVERSATION, conv.id)
    part_pub = await _agent_reply(client, tok, conv_pub, "reply")
    with pytest.raises(service.SuppressedRecipient):
        await service.send_email(
            workspace_id=ws_id,
            conversation_id=conv.id,
            part_id=decode_public_id(IdPrefix.PART, part_pub),
        )
    assert sender.fake_sender().sent == []  # nothing sent
    async with session_scope(ws_id) as s:
        blocked = list(
            (
                await s.scalars(
                    select(EmailDeliveryEvent).where(EmailDeliveryEvent.event == "blocked")
                )
            ).all()
        )
    assert len(blocked) == 1  # recorded in the nested txn before the raise


async def test_soft_bounce_not_suppressed(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "SoftBounce")
    await _account(client, tok, "support@soft.io")
    soft = {
        "notificationType": "Bounce",
        "mail": {"source": "support@soft.io"},
        "bounce": {"bounceType": "Transient", "bouncedRecipients": [{"emailAddress": "a@x.com"}]},
    }
    assert await service.record_ses_event(message_json=json.dumps(soft)) == "soft_bounce_ignored"


# --- Attachment size cap ------------------------------------------------------


async def test_outbound_too_large_rejected(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tok, ws_pub = await _owner(client, "TooBig")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    await _account(client, tok, "support@big.io")
    raw = _inbound_raw(
        frm="alice@example.com",
        to="support@big.io",
        subject="Hi",
        body="q",
        message_id="<big-in@example.com>",
    )
    await _ingest(raw, f"sns-{uuid4().hex}")
    conv = await _only_conversation(ws_id)
    conv_pub = encode_public_id(IdPrefix.CONVERSATION, conv.id)
    part_pub = await _agent_reply(client, tok, conv_pub, "x" * 500)

    monkeypatch.setattr(get_settings(), "email_max_message_bytes", 50)
    with pytest.raises(service.MessageTooLarge):
        await service.send_email(
            workspace_id=ws_id,
            conversation_id=conv.id,
            part_id=decode_public_id(IdPrefix.PART, part_pub),
        )
    assert sender.fake_sender().sent == []


async def test_inbound_oversized_attachment_omitted(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tok, ws_pub = await _owner(client, "BigAttach")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    await _account(client, tok, "support@attach.io")
    monkeypatch.setattr(get_settings(), "email_max_message_bytes", 50)

    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "support@attach.io"
    msg["Subject"] = "big file"
    msg["Message-ID"] = "<att1@example.com>"
    msg.set_content("see attached")
    msg.add_attachment(b"x" * 1000, maintype="application", subtype="pdf", filename="big.pdf")

    assert await _ingest(msg.as_bytes(), f"sns-{uuid4().hex}") == "ingested"
    conv = await _only_conversation(ws_id)
    async with session_scope(ws_id) as s:
        part = (
            await s.scalars(
                select(ConversationPart).where(ConversationPart.conversation_id == conv.id)
            )
        ).first()
    assert part is not None
    assert part.attachments == []  # dropped
    assert "omitted" in (part.body or "")


# --- Outbound exactly-once (double-send chaos) --------------------------------


async def test_double_send_is_exactly_once(client: httpx.AsyncClient) -> None:
    tok, ws_pub = await _owner(client, "ExactlyOnce")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    await _account(client, tok, "support@once.io")
    raw = _inbound_raw(
        frm="alice@example.com",
        to="support@once.io",
        subject="Hi",
        body="q",
        message_id="<once-in@example.com>",
    )
    await _ingest(raw, f"sns-{uuid4().hex}")
    conv = await _only_conversation(ws_id)
    conv_pub = encode_public_id(IdPrefix.CONVERSATION, conv.id)
    part_id = decode_public_id(IdPrefix.PART, await _agent_reply(client, tok, conv_pub, "hi"))

    first = await service.send_email(workspace_id=ws_id, conversation_id=conv.id, part_id=part_id)
    second = await service.send_email(workspace_id=ws_id, conversation_id=conv.id, part_id=part_id)
    assert first == "sent"
    assert second == "already_sent"
    assert len(sender.fake_sender().sent) == 1  # the email left the building exactly once


async def test_concurrent_send_is_exactly_once(client: httpx.AsyncClient) -> None:
    """Two workers racing the same part: the claim-before-send DB gate serializes them so the
    provider is invoked exactly once (review C1)."""
    tok, ws_pub = await _owner(client, "Concurrent")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    await _account(client, tok, "support@concurrent.io")
    raw = _inbound_raw(
        frm="alice@example.com",
        to="support@concurrent.io",
        subject="Hi",
        body="q",
        message_id="<cc-in@example.com>",
    )
    await _ingest(raw, f"sns-{uuid4().hex}")
    conv = await _only_conversation(ws_id)
    conv_pub = encode_public_id(IdPrefix.CONVERSATION, conv.id)
    part_id = decode_public_id(IdPrefix.PART, await _agent_reply(client, tok, conv_pub, "hi"))

    results = await asyncio.gather(
        service.send_email(workspace_id=ws_id, conversation_id=conv.id, part_id=part_id),
        service.send_email(workspace_id=ws_id, conversation_id=conv.id, part_id=part_id),
    )
    assert sorted(results) == ["already_sent", "sent"]
    assert len(sender.fake_sender().sent) == 1  # never double-emailed under concurrency


# --- Pause switch -------------------------------------------------------------


async def test_paused_account_blocks_send(client: httpx.AsyncClient) -> None:
    tok, ws_pub = await _owner(client, "Paused")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    account = await _account(client, tok, "support@paused.io")
    raw = _inbound_raw(
        frm="alice@example.com",
        to="support@paused.io",
        subject="Hi",
        body="q",
        message_id="<p-in@example.com>",
    )
    await _ingest(raw, f"sns-{uuid4().hex}")
    conv = await _only_conversation(ws_id)
    conv_pub = encode_public_id(IdPrefix.CONVERSATION, conv.id)
    part_id = decode_public_id(IdPrefix.PART, await _agent_reply(client, tok, conv_pub, "hi"))

    r = await client.post(
        f"/v0/channels/email/accounts/{account['id']}/status",
        json={"status": "paused"},
        headers=_auth(tok),
    )
    assert r.status_code == 200, r.text

    assert (
        await service.send_email(workspace_id=ws_id, conversation_id=conv.id, part_id=part_id)
        == "paused"
    )
    assert sender.fake_sender().sent == []


# --- Dispatcher filtering -----------------------------------------------------


def _drain_outbox_to_stream() -> None:
    from relay.core import outbox_relay

    dsn = get_settings().database_url_psycopg
    redis = get_redis_sync()
    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        outbox_relay.drain(conn, redis)


async def test_dispatch_enqueues_only_email_agent_replies(client: httpx.AsyncClient) -> None:
    tok, ws_pub = await _owner(client, "Dispatch")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    await _account(client, tok, "support@dispatch.io")
    raw = _inbound_raw(
        frm="alice@example.com",
        to="support@dispatch.io",
        subject="Hi",
        body="q",
        message_id="<d-in@example.com>",
    )
    await _ingest(raw, f"sns-{uuid4().hex}")
    conv = await _only_conversation(ws_id)
    conv_pub = encode_public_id(IdPrefix.CONVERSATION, conv.id)
    part_pub = await _agent_reply(client, tok, conv_pub, "reply body")
    part_id = str(decode_public_id(IdPrefix.PART, part_pub))

    _drain_outbox_to_stream()

    enqueued: list[tuple[str, str, str]] = []

    async def fake_enqueue(ws: str, cv: str, pt: str) -> None:
        enqueued.append((ws, cv, pt))

    redis = get_redis()
    await dispatch.ensure_group(redis)
    await dispatch.consume_once(redis, fake_enqueue, count=1000)

    # The admin email reply is enqueued; the inbound contact comment (author_kind=contact) is not.
    assert (str(ws_id), str(conv.id), part_id) in enqueued
    parts_enqueued = [e for e in enqueued if e[1] == str(conv.id)]
    assert parts_enqueued == [(str(ws_id), str(conv.id), part_id)]


# --- Domain verification ------------------------------------------------------


async def test_domain_create_and_verify(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tok, _ws = await _owner(client, "Domains")
    r = await client.post(
        "/v0/channels/email/domains", json={"domain": "mail.acme.io"}, headers=_auth(tok)
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["status"] == "pending"
    assert any(rec["purpose"] == "spf" for rec in created["dns_records"])

    monkeypatch.setattr(service, "check_domain_verified", lambda _domain: True)
    verified = (
        await client.post(f"/v0/channels/email/domains/{created['id']}/verify", headers=_auth(tok))
    ).json()
    assert verified["status"] == "verified"
    assert verified["verified_at"] is not None


# --- Unroutable ⇒ DLQ ---------------------------------------------------------


async def test_unroutable_email_raises(client: httpx.AsyncClient) -> None:
    # Recipient address maps to no channel account in any workspace.
    raw = _inbound_raw(
        frm="alice@example.com",
        to="nobody@unknown.io",
        subject="Hi",
        body="q",
        message_id="<u-in@example.com>",
    )
    with pytest.raises(service.UnroutableEmail):
        await _ingest(raw, f"sns-{uuid4().hex}")


# --- Cross-tenant isolation ---------------------------------------------------


async def test_cross_tenant_suppression_isolation(client: httpx.AsyncClient) -> None:
    tok_a, _ws_a = await _owner(client, "TenantA")
    tok_b, _ws_b = await _owner(client, "TenantB")
    ra = await client.post(
        "/v0/channels/email/suppressions",
        json={"email": "blocked@a.com"},
        headers=_auth(tok_a),
    )
    assert ra.status_code == 201, ra.text

    a_list = (await client.get("/v0/channels/email/suppressions", headers=_auth(tok_a))).json()
    b_list = (await client.get("/v0/channels/email/suppressions", headers=_auth(tok_b))).json()
    assert [i["email"] for i in a_list["items"]] == ["blocked@a.com"]
    assert b_list["items"] == []  # tenant B sees nothing of tenant A's suppressions


async def test_global_address_uniqueness_across_tenants(client: httpx.AsyncClient) -> None:
    tok_a, _a = await _owner(client, "AddrA")
    tok_b, _b = await _owner(client, "AddrB")
    await _account(client, tok_a, "support@shared.io")
    r = await client.post(
        "/v0/channels/email/accounts",
        json={"address": "support@shared.io"},
        headers=_auth(tok_b),
    )
    assert r.status_code == 409  # generic "unavailable" — no cross-tenant leak
    assert "unavailable" in r.json()["error"]["message"]


# --- Reopen a closed conversation on inbound reply (through the state machine) -


async def test_inbound_reply_reopens_closed_conversation(client: httpx.AsyncClient) -> None:
    tok, ws_pub = await _owner(client, "Reopen")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    await _account(client, tok, "support@reopen.io")
    conv, out = await _round_trip_to_outbound(client, tok, ws_id, "support@reopen.io")
    conv_pub = encode_public_id(IdPrefix.CONVERSATION, conv.id)

    r = await client.post(
        f"/v0/conversations/{conv_pub}/state", json={"state": "closed"}, headers=_auth(tok)
    )
    assert r.status_code == 200 and r.json()["state"] == "closed"

    reply = _inbound_raw(
        frm="alice@example.com",
        to=out["Reply-To"],
        subject="Re: Help",
        body="still broken",
        message_id="<reopen-r1@example.com>",
    )
    assert await _ingest(reply, f"sns-{uuid4().hex}") == "ingested"

    async with session_scope(ws_id) as s:
        reloaded = await s.get(Conversation, conv.id)
        assert reloaded is not None
        assert reloaded.state == "open"  # reopened
        state_changes = list(
            (
                await s.scalars(
                    select(ConversationPart).where(
                        ConversationPart.conversation_id == conv.id,
                        ConversationPart.part_type == "state_change",
                    )
                )
            ).all()
        )
    # The reopen went through W1 (a system state_change part with the inbound-reply reason).
    assert any(p.meta.get("reason") == "inbound_reply" for p in state_changes)


# --- Security: routing uses the SES envelope, not forgeable To/Cc (finding 3) -


async def test_cc_injection_ignored_when_envelope_recipients_given(
    client: httpx.AsyncClient,
) -> None:
    tok, ws_pub = await _owner(client, "CcInject")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    await _account(client, tok, "support@ccinj.io")

    # Attacker forges To=support@ccinj.io but the SES envelope delivered only to the attacker's own
    # address → must NOT route to the victim workspace (no cross-tenant conversation injection).
    forged = _inbound_raw(
        frm="attacker@evil.com",
        to="support@ccinj.io",
        subject="inject",
        body="x",
        message_id="<cc1@evil.com>",
    )
    with pytest.raises(service.UnroutableEmail):
        await _ingest(forged, f"sns-{uuid4().hex}", recipients=["attacker@evil.com"])
    async with session_scope(ws_id) as s:
        count = await s.scalar(select(func.count()).select_from(Conversation))
    assert count == 0  # victim workspace untouched

    # Control: the true envelope recipient routes normally.
    legit = _inbound_raw(
        frm="cust@example.com",
        to="support@ccinj.io",
        subject="hi",
        body="y",
        message_id="<cc2@example.com>",
    )
    assert await _ingest(legit, f"sns-{uuid4().hex}", recipients=["support@ccinj.io"]) == "ingested"


# --- Exactly-once: provider failure rolls the claim back, then re-sends (finding 5) -


async def test_send_rollback_on_provider_failure_then_resends(client: httpx.AsyncClient) -> None:
    tok, ws_pub = await _owner(client, "SendFail")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    await _account(client, tok, "support@sendfail.io")
    raw = _inbound_raw(
        frm="alice@example.com",
        to="support@sendfail.io",
        subject="Hi",
        body="q",
        message_id="<sf-in@example.com>",
    )
    await _ingest(raw, f"sns-{uuid4().hex}", recipients=["support@sendfail.io"])
    conv = await _only_conversation(ws_id)
    conv_pub = encode_public_id(IdPrefix.CONVERSATION, conv.id)
    part_id = decode_public_id(IdPrefix.PART, await _agent_reply(client, tok, conv_pub, "reply"))

    # Force one transient provider failure: the claim must roll back (no 'out' row, nothing sent).
    sender.fake_sender().fail_next = 1
    with pytest.raises(sender.SendError):
        await service.send_email(workspace_id=ws_id, conversation_id=conv.id, part_id=part_id)
    async with session_scope(ws_id) as s:
        out_rows = list(
            (
                await s.scalars(
                    select(EmailLedger).where(
                        EmailLedger.part_id == part_id, EmailLedger.direction == "out"
                    )
                )
            ).all()
        )
    assert out_rows == []  # claim rolled back with the failed txn
    assert sender.fake_sender().sent == []

    # Retry succeeds and sends exactly once (never lost, never doubled).
    assert (
        await service.send_email(workspace_id=ws_id, conversation_id=conv.id, part_id=part_id)
        == "sent"
    )
    assert len(sender.fake_sender().sent) == 1


# --- Compliance: bounce/complaint suppresses even for a paused account (finding 7) -


async def test_paused_account_bounce_still_suppresses(client: httpx.AsyncClient) -> None:
    tok, ws_pub = await _owner(client, "PausedBounce")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    account = await _account(client, tok, "support@pausedbounce.io")
    r = await client.post(
        f"/v0/channels/email/accounts/{account['id']}/status",
        json={"status": "paused"},
        headers=_auth(tok),
    )
    assert r.status_code == 200, r.text

    bounce = {
        "notificationType": "Bounce",
        "mail": {"source": "support@pausedbounce.io"},
        "bounce": {
            "bounceType": "Permanent",
            "bouncedRecipients": [{"emailAddress": "gone@example.com"}],
        },
    }
    assert await service.record_ses_event(message_json=json.dumps(bounce)) == "suppressed"
    async with session_scope(ws_id) as s:
        supp = list((await s.scalars(select(Suppression))).all())
    assert [x.email for x in supp] == ["gone@example.com"]

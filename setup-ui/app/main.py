"""
Bridgerton Setup UI
──────────────────
FastAPI service that:
  • Logs into Matrix as the admin user
  • Drives the WhatsApp / Signal bridge bots via their command API
  • Streams QR codes and status updates to the browser via SSE
  • Runs a relay loop that forwards messages between mapped portal rooms
"""

import asyncio
import base64
import io
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
import qrcode
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Config ───────────────────────────────────────────────────────────────────

MATRIX_URL    = os.environ.get("MATRIX_SERVER_URL", "http://synapse:8008")
DOMAIN        = os.environ.get("MATRIX_SERVER_NAME", "bridge.local")
ADMIN_USER    = os.environ.get("SYNAPSE_ADMIN_USER", "admin")
ADMIN_PASS    = os.environ.get("SYNAPSE_ADMIN_PASSWORD", "")
ADMIN_MXID    = f"@{ADMIN_USER}:{DOMAIN}"
WA_BOT        = f"@whatsappbot:{DOMAIN}"
SIG_BOT       = f"@signalbot:{DOMAIN}"
DATA_DIR      = Path(os.environ.get("DATA_DIR", "/data"))
RELAY_FILE    = DATA_DIR / "relays.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Shared state (single process / single event loop) ────────────────────────

st: dict[str, Any] = {
    "token":      None,   # Matrix access token
    "sync_token": None,   # next_batch
    "whatsapp": {
        "status":  "unlinked",   # unlinked | waiting | qr_ready | linked
        "qr":      None,         # base64 PNG
        "user":    None,         # "Logged in as …"
        "dm_room": None,
    },
    "signal": {
        "status":   "unlinked",
        "qr":       None,
        "link_uri": None,
        "user":     None,
        "dm_room":  None,
    },
    "rooms": {"whatsapp": [], "signal": []},
    "seen_events":  set(),   # avoid processing duplicates / echoes
    "relayed_ids":  set(),   # event IDs we sent (echo guard)
    "event_mirror": {},      # source_event_id ↔ relay_event_id (bidirectional)
    # Relay sender context: (from_room_id, target_room_id) → last sender_mxid
    # Used to suppress repeated headers for the same consecutive sender.
    # Keyed by (from_room, target) so Signal "John" and WhatsApp "John" are
    # always treated as distinct senders.
    "relay_context": {},
    "sse_subs":     [],      # list of asyncio.Queue per SSE client
    "bg_tasks":     set(),   # strong references keep tasks alive
}


def _spawn(coro) -> asyncio.Task:
    """Create a background task and keep a strong reference so GC can't kill it."""
    task = asyncio.create_task(coro)
    st["bg_tasks"].add(task)
    task.add_done_callback(st["bg_tasks"].discard)
    return task


def _sse_push(event: dict) -> None:
    """Broadcast to every connected SSE subscriber."""
    dead = []
    for q in st["sse_subs"]:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        st["sse_subs"].remove(q)


# ── Persistence ───────────────────────────────────────────────────────────────

def load_relays() -> list:
    try:
        return json.loads(RELAY_FILE.read_text())
    except Exception:
        return []


def save_relays(relays: list) -> None:
    RELAY_FILE.write_text(json.dumps(relays, indent=2))


# ── Matrix HTTP helpers ───────────────────────────────────────────────────────

async def mx(session: aiohttp.ClientSession, method: str, path: str, **kwargs):
    """Authenticated Matrix API call. Returns (status, body)."""
    headers = {"Authorization": f"Bearer {st['token']}"} if st["token"] else {}
    url = f"{MATRIX_URL}/_matrix/client/v3{path}"
    async with session.request(method, url, headers=headers, **kwargs) as r:
        try:
            body = await r.json()
        except Exception:
            body = {}
        if r.status >= 400:
            log.debug("Matrix %s %s → %s %s", method, path, r.status, body)
        return r.status, body


async def mx_download(session: aiohttp.ClientSession, mxc: str) -> bytes | None:
    """Download an mxc:// media URL. Returns raw bytes or None."""
    m = re.match(r"mxc://([^/]+)/(.+)", mxc)
    if not m:
        return None
    server, media_id = m.groups()
    headers = {"Authorization": f"Bearer {st['token']}"}
    # Synapse 1.104+ uses the authenticated v1 endpoint; fall back to legacy v3
    for url in [
        f"{MATRIX_URL}/_matrix/client/v1/media/download/{server}/{media_id}",
        f"{MATRIX_URL}/_matrix/media/v3/download/{server}/{media_id}",
    ]:
        try:
            async with session.get(url, headers=headers) as r:
                if r.status == 200:
                    return await r.read()
                log.debug("Media download %s → %s", url, r.status)
        except Exception as e:
            log.debug("Media download error %s: %s", url, e)
    return None


def make_qr(data: str) -> str:
    """Generate a base64-encoded PNG QR code from a string."""
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


async def send_text(session: aiohttp.ClientSession, room_id: str, text: str) -> str | None:
    """Send a text message. Returns the new event_id or None."""
    txn = uuid.uuid4().hex
    status, resp = await mx(
        session, "PUT",
        f"/rooms/{room_id}/send/m.room.message/{txn}",
        json={"msgtype": "m.text", "body": text},
    )
    event_id = resp.get("event_id")
    if event_id:
        st["relayed_ids"].add(event_id)
    return event_id


async def send_event(session: aiohttp.ClientSession, room_id: str, event_type: str,
                     content: dict, *, source_event_id: str | None = None) -> str | None:
    """Send any Matrix event type to a room. Returns the new event_id or None."""
    txn = uuid.uuid4().hex
    status, resp = await mx(
        session, "PUT",
        f"/rooms/{room_id}/send/{event_type}/{txn}",
        json=content,
    )
    event_id = resp.get("event_id")
    if event_id:
        st["relayed_ids"].add(event_id)
        if source_event_id:
            st["event_mirror"][source_event_id] = event_id
            st["event_mirror"][event_id] = source_event_id
    return event_id


async def send_content(session: aiohttp.ClientSession, room_id: str, content: dict,
                       *, source_event_id: str | None = None) -> str | None:
    """Forward an arbitrary m.room.message content dict. Returns the new event_id or None."""
    return await send_event(session, room_id, "m.room.message", content,
                            source_event_id=source_event_id)


async def send_redaction(session: aiohttp.ClientSession, room_id: str, event_id: str) -> None:
    txn = uuid.uuid4().hex
    await mx(session, "PUT", f"/rooms/{room_id}/redact/{event_id}/{txn}", json={})


async def send_header(session: aiohttp.ClientSession, room_id: str, text: str) -> None:
    """Send a plain-text / HTML sender attribution line (not tracked in relayed_ids)."""
    txn = uuid.uuid4().hex
    await mx(session, "PUT", f"/rooms/{room_id}/send/m.room.message/{txn}", json={
        "msgtype": "m.text",
        "body": text,
        "format": "org.matrix.custom.html",
        "formatted_body": f"<strong>{text}</strong>",
    })


def _relay_context_changed(from_room: str, target: str, sender: str) -> bool:
    """Return True when a sender header should be shown (new sender or context was broken)."""
    return st["relay_context"].get((from_room, target)) != sender


def _update_relay_context(from_room: str, target: str, sender: str) -> None:
    """Record the sender for this direction and break the opposite-direction context.

    Breaking the opposite direction means: we just placed a bot message in *target*,
    so the next message forwarded from target back to from_room needs a fresh header.
    """
    st["relay_context"][(from_room, target)] = sender
    st["relay_context"][(target, from_room)] = None


TOKEN_FILE = DATA_DIR / "matrix_token.json"


async def login(session: aiohttp.ClientSession) -> bool:
    """Login to Matrix, reusing a persisted token when possible."""
    # Try persisted token first
    if TOKEN_FILE.exists():
        try:
            saved = json.loads(TOKEN_FILE.read_text())
            st["token"] = saved["access_token"]
            # Validate it with a whoami call
            code, resp = await mx(session, "GET", "/account/whoami")
            if code == 200 and resp.get("user_id") == ADMIN_MXID:
                log.info("Reused persisted Matrix token for %s", ADMIN_MXID)
                return True
            st["token"] = None  # invalid, fall through to fresh login
        except Exception:
            st["token"] = None

    status, resp = await mx(session, "POST", "/login", json={
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": ADMIN_USER},
        "password": ADMIN_PASS,
    })
    if status == 200:
        st["token"] = resp["access_token"]
        TOKEN_FILE.write_text(json.dumps({"access_token": resp["access_token"]}))
        log.info("Logged into Matrix as %s (fresh token)", ADMIN_MXID)
        return True
    log.warning("Matrix login failed (%s): %s", status, resp)
    return False


async def get_or_create_dm(session: aiohttp.ClientSession, bot_mxid: str) -> str | None:
    """Return the existing DM room with bot_mxid, or create one."""
    _, resp = await mx(session, "GET", "/joined_rooms")
    for room_id in resp.get("joined_rooms", []):
        _, members_resp = await mx(session, "GET", f"/rooms/{room_id}/joined_members")
        mxids = set(members_resp.get("joined", {}).keys())
        if mxids == {ADMIN_MXID, bot_mxid}:
            # Skip space rooms — they can have the same member set as a DM
            _, create_ev = await mx(session, "GET", f"/rooms/{room_id}/state/m.room.create")
            if create_ev.get("type") == "m.space":
                continue
            return room_id

    _, resp = await mx(session, "POST", "/createRoom", json={
        "is_direct": True,
        "invite": [bot_mxid],
        "preset": "private_chat",
    })
    room_id = resp.get("room_id")
    if room_id:
        # Wait for bot to auto-join
        for _ in range(10):
            await asyncio.sleep(1)
            _, m = await mx(session, "GET", f"/rooms/{room_id}/joined_members")
            if bot_mxid in m.get("joined", {}):
                break
    return room_id


async def get_display_name(session: aiohttp.ClientSession, room_id: str, user_id: str) -> str:
    _, resp = await mx(session, "GET", f"/rooms/{room_id}/state/m.room.member/{user_id}")
    name = resp.get("displayname") or resp.get("display_name")
    if name:
        return name
    # Fall back to user-local part
    return user_id.split(":")[0].lstrip("@")


# ── Event processors ─────────────────────────────────────────────────────────

async def handle_bot_dm(session: aiohttp.ClientSession, room_id: str, sender: str, content: dict):
    """Process events arriving in the management DM rooms with bridge bots."""
    wa = st["whatsapp"]
    sig = st["signal"]
    msgtype = content.get("msgtype", "")
    body = content.get("body", "")

    # ── WhatsApp management room ─────────────────────────────────────────────
    if room_id == wa["dm_room"] and sender == WA_BOT:
        log.info("WA DM event: msgtype=%s body=%s", msgtype, body[:80])
        if msgtype == "m.image":
            mxc = content.get("url", "")
            log.info("Downloading WA QR image: %s", mxc)
            data = await mx_download(session, mxc)
            if data:
                wa["qr"] = base64.b64encode(data).decode()
                wa["status"] = "qr_ready"
                _sse_push({"type": "wa_qr", "qr": wa["qr"]})
                log.info("WhatsApp QR received (%d bytes)", len(data))
            else:
                log.warning("WA QR download failed for %s", mxc)

        elif msgtype in ("m.text", "m.notice"):
            # Bridges send status updates as m.notice in the megabridge format
            low = body.lower()
            log.info("WA bot notice: %s", body[:200])
            if "not logged in" in low or "you're not logged in" in low:
                wa.update(status="unlinked")
                _sse_push({"type": "wa_status", "status": "unlinked"})
            elif any(k in low for k in ("successfully logged in", "logged in as", "connected")):
                wa.update(status="linked", user=body, qr=None)
                _sse_push({"type": "wa_linked", "user": body})
                log.info("WhatsApp linked: %s", body)
                _spawn(_refresh_rooms())
                _spawn(_sync_whatsapp_portals(session))
            elif any(k in low for k in ("error", "failed", "cancel", "timed out", "timeout")):
                wa.update(status="unlinked", qr=None)
                _sse_push({"type": "wa_error", "msg": body})

    # ── Signal management room ───────────────────────────────────────────────
    if room_id == sig["dm_room"] and sender == SIG_BOT:
        log.info("Signal DM event: msgtype=%s body=%s", msgtype, body[:80])
        if msgtype == "m.image":
            mxc = content.get("url", "")
            log.info("Downloading Signal QR image: %s", mxc)
            data = await mx_download(session, mxc)
            if data:
                sig["qr"] = base64.b64encode(data).decode()
                sig["status"] = "qr_ready"
                _sse_push({"type": "sig_qr", "qr": sig["qr"]})
                log.info("Signal QR image received (%d bytes)", len(data))
            else:
                log.warning("Signal QR download failed for %s", mxc)

        elif msgtype in ("m.text", "m.notice"):
            low = body.lower()
            log.info("Signal bot notice: %s", body[:200])
            uri_m = re.search(r"sgnl://[^\s>]+", body)
            if uri_m:
                uri = uri_m.group(0)
                sig.update(link_uri=uri, qr=make_qr(uri), status="qr_ready")
                _sse_push({"type": "sig_qr", "qr": sig["qr"], "uri": uri})
                log.info("Signal link URI received")
            elif "not logged in" in low:
                sig.update(status="unlinked")
                _sse_push({"type": "sig_status", "status": "unlinked"})
            elif any(k in low for k in ("successfully logged in", "logged in as", "connected")):
                sig.update(status="linked", user=body, qr=None, link_uri=None)
                _sse_push({"type": "sig_linked", "user": body})
                log.info("Signal linked: %s", body)
                _spawn(_refresh_rooms())
                # Auto-create portals for all existing Signal groups so the user
                # doesn't have to run !signal list-portals / create-portal manually.
                _spawn(_sync_signal_portals(session))
            elif "successfully created portal" in low or "successfully bridged" in low:
                _spawn(_refresh_rooms())
            elif any(k in low for k in ("error", "failed", "cancel")):
                sig.update(status="unlinked", qr=None)
                _sse_push({"type": "sig_error", "msg": body})


# msgtypes that forward directly as m.room.message
RELAYABLE_MSG_TYPES = {"m.text", "m.image", "m.video", "m.audio", "m.file", "m.location"}

# Poll event types (forwarded as text fallback)
POLL_EVENT_TYPES = {
    "org.matrix.msc3381.poll.start",
    "m.poll.start",
    "net.maunium.whatsapp.poll_creation",
}

# All event types we actively handle in relay rooms
RELAY_EVENT_TYPES = {"m.room.message", "m.sticker", "m.reaction"} | POLL_EVENT_TYPES


async def handle_relay(session: aiohttp.ClientSession, from_room: str, event: dict,
                       sender: str, content: dict, event_type: str = "m.room.message"):
    """Forward a message from one portal room to its mapped counterpart."""
    if sender in (ADMIN_MXID, WA_BOT, SIG_BOT):
        return

    relays = load_relays()
    target = None
    for r in relays:
        if r["signal_room"] == from_room:
            target = r["whatsapp_room"]
            break
        if r["whatsapp_room"] == from_room:
            target = r["signal_room"]
            break
    if not target:
        return

    source_event_id = event.get("event_id")
    display_name = await get_display_name(session, from_room, sender)

    # ── Reactions ──────────────────────────────────────────────────────────────
    # The bot can only hold one reaction per event, so reactions are sent as a
    # plain text notice instead of an m.reaction event.
    if event_type == "m.reaction":
        relates_to  = content.get("m.relates_to", {})
        emoji       = relates_to.get("key", "?")
        reacted_to  = relates_to.get("event_id")
        body        = f"{display_name} reacted with {emoji}"
        reaction_content: dict = {
            "msgtype": "m.text",
            "body": body,
            "format": "org.matrix.custom.html",
            "formatted_body": f"<em>{body}</em>",
        }
        # Link back to the mirrored message so the reaction appears as a reply.
        if reacted_to:
            mirror = st["event_mirror"].get(reacted_to)
            if mirror:
                reaction_content["m.relates_to"] = {"m.in_reply_to": {"event_id": mirror}}
        await send_event(session, target, "m.room.message", reaction_content,
                         source_event_id=source_event_id)
        _update_relay_context(from_room, target, sender)
        log.info("Reaction notice %s → %s: %s %s", from_room[:20], target[:20], display_name, emoji)
        return

    # ── Stickers ───────────────────────────────────────────────────────────────
    # Always announce who sent the sticker, then forward the sticker event.
    if event_type == "m.sticker":
        header = f"{display_name} sent a sticker:"
        await send_header(session, target, header)
        fwd = {k: v for k, v in content.items() if k not in ("m.relates_to",)}
        await send_event(session, target, "m.sticker", fwd, source_event_id=source_event_id)
        _update_relay_context(from_room, target, sender)
        log.info("Relayed sticker %s → %s", from_room[:20], target[:20])
        return

    # ── Polls ──────────────────────────────────────────────────────────────────
    if event_type in POLL_EVENT_TYPES:
        question = (content.get("question") or {}).get("body", "") or content.get("body", "")
        answers  = content.get("answers", content.get("options", []))
        first    = f"{display_name} sent a poll: {question}" if question else f"{display_name} sent a poll"
        lines    = [first]
        for a in answers[:10]:
            lines.append("  - " + (a.get("body", str(a)) if isinstance(a, dict) else str(a)))
        await send_event(session, target, "m.room.message",
                         {"msgtype": "m.text", "body": "\n".join(lines)},
                         source_event_id=source_event_id)
        _update_relay_context(from_room, target, sender)
        log.info("Sent poll fallback %s → %s", from_room[:20], target[:20])
        return

    # ── m.room.message ─────────────────────────────────────────────────────────
    # Translate reply IDs and strip edit relations.
    msgtype = content.get("msgtype", "")
    fwd = {k: v for k, v in content.items() if k not in ("m.relates_to", "m.new_content")}
    relates    = content.get("m.relates_to", {})
    replied_to = relates.get("m.in_reply_to", {}).get("event_id")
    if replied_to:
        mirror = st["event_mirror"].get(replied_to)
        if mirror:
            fwd["m.relates_to"] = {"m.in_reply_to": {"event_id": mirror}}

    needs_header = _relay_context_changed(from_room, target, sender)
    _update_relay_context(from_room, target, sender)

    if msgtype == "m.text":
        # Embed the sender name directly in the body when context has changed.
        if needs_header:
            text = content.get("body", "")
            fwd["body"] = f"{display_name}: {text}"
            if "formatted_body" in fwd or "format" in fwd:
                fwd["formatted_body"] = (
                    f"<strong>{display_name}:</strong> "
                    + (content.get("formatted_body") or text)
                )
                fwd["format"] = "org.matrix.custom.html"
        await send_event(session, target, "m.room.message", fwd, source_event_id=source_event_id)
        log.info("Relayed m.text → %s: %s", target[:20], fwd["body"][:60])

    elif msgtype in RELAYABLE_MSG_TYPES:
        # Attachments: send a separate header message first if context changed.
        if needs_header:
            await send_header(session, target, f"{display_name}:")
        await send_event(session, target, "m.room.message", fwd, source_event_id=source_event_id)
        log.info("Relayed %s (%s) → %s", from_room[:20], msgtype, target[:20])

    else:
        # Unknown/unsupported msgtype — notify recipient so they know a message was missed.
        body     = content.get("body", "")
        fallback = f"[Unsupported message type: {msgtype}]"
        if body:
            fallback += f" {body}"
        if needs_header:
            fallback = f"{display_name}: {fallback}"
        await send_event(session, target, "m.room.message",
                         {"msgtype": "m.text", "body": fallback},
                         source_event_id=source_event_id)
        log.info("Fallback for unsupported %s in %s → %s", msgtype, from_room[:20], target[:20])


async def process_sync_response(session: aiohttp.ClientSession, sync_resp: dict):
    relayed_rooms = {r["signal_room"] for r in load_relays()} | \
                    {r["whatsapp_room"] for r in load_relays()}
    dm_rooms = {st["whatsapp"]["dm_room"], st["signal"]["dm_room"]} - {None}

    # Auto-accept invites from bridge bots into portal rooms.
    # Check that the bridge bot is already a joined member in the invite_state —
    # group portal invites may arrive from ghost/puppet users, not the bot itself.
    for room_id, invite_data in sync_resp.get("rooms", {}).get("invite", {}).items():
        events = invite_data.get("invite_state", {}).get("events", [])
        bot_in_room = any(
            e.get("type") == "m.room.member"
            and e.get("state_key") in (WA_BOT, SIG_BOT)
            and e.get("content", {}).get("membership") == "join"
            for e in events
        )
        if bot_in_room:
            _, r = await mx(session, "POST", f"/join/{room_id}")
            log.info("Auto-joined portal room %s (bridge bot present)", room_id)

    for room_id, room_data in sync_resp.get("rooms", {}).get("join", {}).items():
        for event in room_data.get("timeline", {}).get("events", []):
            eid = event.get("event_id")
            if not eid or eid in st["seen_events"] or eid in st["relayed_ids"]:
                continue
            st["seen_events"].add(eid)
            # Trim set to avoid unbounded growth
            if len(st["seen_events"]) > 10_000:
                st["seen_events"] = set(list(st["seen_events"])[5_000:])

            sender     = event.get("sender", "")
            content    = event.get("content", {})
            event_type = event.get("type", "")

            # Propagate redactions across relay pairs
            if event_type == "m.room.redaction" and room_id in relayed_rooms:
                if sender not in (ADMIN_MXID, WA_BOT, SIG_BOT):
                    redacted_id = event.get("redacts") or content.get("redacts")
                    mirror_id   = st["event_mirror"].get(redacted_id) if redacted_id else None
                    if mirror_id:
                        target = next(
                            (r["whatsapp_room"] if r["signal_room"] == room_id else r["signal_room"]
                             for r in load_relays()
                             if r["signal_room"] == room_id or r["whatsapp_room"] == room_id),
                            None,
                        )
                        if target:
                            await send_redaction(session, target, mirror_id)
                            log.info("Relayed redaction → %s", target[:20])
                continue

            # Forward messages, stickers, and polls between relay room pairs
            if room_id in relayed_rooms and event_type in RELAY_EVENT_TYPES:
                log.info("Sync: relay event in %s from %s type=%s msgtype=%s body=%s",
                         room_id[:20], sender, event_type,
                         content.get("msgtype"), content.get("body", "")[:60])
                await handle_relay(session, room_id, event, sender, content, event_type)

            # Management DM events (only m.room.message)
            if event_type == "m.room.message" and room_id in dm_rooms:
                log.info("Sync: DM event in %s from %s", room_id[:20], sender)
                await handle_bot_dm(session, room_id, sender, content)


async def _refresh_rooms():
    """List portal rooms joined by the admin user for each bridge."""
    await asyncio.sleep(4)   # Give bridges time to create portal rooms
    if not st["token"]:
        return
    async with aiohttp.ClientSession() as session:
        _, resp = await mx(session, "GET", "/joined_rooms")
        joined = resp.get("joined_rooms", [])
        ignore = {st["whatsapp"]["dm_room"], st["signal"]["dm_room"]} - {None}

        wa_rooms, sig_rooms = [], []
        for room_id in joined:
            if room_id in ignore:
                continue
            _, members = await mx(session, "GET", f"/rooms/{room_id}/joined_members")
            joined_ids = set(members.get("joined", {}).keys())

            _, name_ev = await mx(session, "GET", f"/rooms/{room_id}/state/m.room.name")
            name = name_ev.get("name") or room_id

            # Skip space/management rooms (type == m.space or name matches management pattern)
            _, creation = await mx(session, "GET", f"/rooms/{room_id}/state/m.room.create")
            if creation.get("type") == "m.space":
                continue

            if WA_BOT in joined_ids:
                wa_rooms.append({"room_id": room_id, "name": name})
            elif SIG_BOT in joined_ids:
                sig_rooms.append({"room_id": room_id, "name": name})

        st["rooms"]["whatsapp"] = sorted(wa_rooms,  key=lambda r: r["name"].lower())
        st["rooms"]["signal"]   = sorted(sig_rooms, key=lambda r: r["name"].lower())
        _sse_push({"type": "rooms_updated",
                   "whatsapp": st["rooms"]["whatsapp"],
                   "signal":   st["rooms"]["signal"]})
        log.info("Rooms refreshed: %d WA, %d Signal",
                 len(wa_rooms), len(sig_rooms))


async def _sync_whatsapp_portals(session: aiohttp.ClientSession):
    """Send !wa sync groups to create portals for all WhatsApp groups."""
    await asyncio.sleep(5)
    room = st["whatsapp"]["dm_room"]
    if not room:
        return
    await send_text(session, room, "!wa sync groups")
    log.info("Sent '!wa sync groups' to auto-create WhatsApp group portals")
    # Give the bridge time to create portals, then refresh the room list
    await asyncio.sleep(15)
    await _refresh_rooms()


async def _sync_signal_portals(_session: aiohttp.ClientSession):
    """The Signal megabridge creates group portals lazily (when the first message
    arrives in a group). Poll periodically so rooms appear as soon as they do."""
    for delay in (20, 40, 90, 180):
        await asyncio.sleep(delay)
        await _refresh_rooms()
    log.info("Signal portal refresh polling complete")


async def _initial_status_check(_ignored=None):
    """Discover existing bridge logins via list-logins on startup."""
    await asyncio.sleep(3)
    try:
        async with aiohttp.ClientSession() as session:
            for bot, cmd, key in [(WA_BOT, "!wa list-logins", "whatsapp"),
                                  (SIG_BOT, "!signal list-logins", "signal")]:
                try:
                    room = await get_or_create_dm(session, bot)
                    if room:
                        st[key]["dm_room"] = room
                        await send_text(session, room, cmd)
                        log.info("Sent %s to %s (room %s)", cmd, bot, room)
                except Exception:
                    log.exception("Initial status check failed for %s", key)
    except Exception:
        log.exception("Initial status check session error")


# ── Background sync loop ──────────────────────────────────────────────────────

async def sync_loop():
    async with aiohttp.ClientSession() as session:
        while not await login(session):
            log.info("Waiting for Synapse…")
            await asyncio.sleep(5)

        # Consume backlog — process invites but skip message history
        _, resp = await mx(session, "GET", "/sync", params={"timeout": 0})
        st["sync_token"] = resp.get("next_batch")
        for room_id, invite_data in resp.get("rooms", {}).get("invite", {}).items():
            events = invite_data.get("invite_state", {}).get("events", [])
            bot_in_room = any(
                e.get("type") == "m.room.member"
                and e.get("state_key") in (WA_BOT, SIG_BOT)
                and e.get("content", {}).get("membership") == "join"
                for e in events
            )
            if bot_in_room:
                _, r = await mx(session, "POST", f"/join/{room_id}")
                log.info("Joined pending portal room %s on startup", room_id)

        _spawn(_initial_status_check())

        while True:
            try:
                params: dict = {"timeout": 30_000}
                if st["sync_token"]:
                    params["since"] = st["sync_token"]
                _, resp = await mx(session, "GET", "/sync", params=params)
                st["sync_token"] = resp.get("next_batch")
                await process_sync_response(session, resp)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Sync error")
                await asyncio.sleep(5)


# ── FastAPI app ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(sync_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    return Path("static/index.html").read_text()


# ── Status / SSE ─────────────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    return {
        "whatsapp": {"status": st["whatsapp"]["status"], "user": st["whatsapp"]["user"]},
        "signal":   {"status": st["signal"]["status"],   "user": st["signal"]["user"]},
        "rooms":    st["rooms"],
        "relays":   load_relays(),
    }


@app.get("/api/events")
async def api_events(request: Request):
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    st["sse_subs"].append(q)

    # Send current state immediately
    init = {
        "type": "init",
        "whatsapp": {"status": st["whatsapp"]["status"], "user": st["whatsapp"]["user"],
                     "qr": st["whatsapp"]["qr"]},
        "signal":   {"status": st["signal"]["status"],   "user": st["signal"]["user"],
                     "qr": st["signal"]["qr"]},
        "rooms":    st["rooms"],
        "relays":   load_relays(),
    }
    q.put_nowait(init)

    async def generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            try:
                st["sse_subs"].remove(q)
            except ValueError:
                pass

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ── Bridge linking ────────────────────────────────────────────────────────────

@app.post("/api/whatsapp/login")
async def api_wa_login():
    if not st["token"]:
        raise HTTPException(503, "Not connected to Matrix yet")
    async with aiohttp.ClientSession() as session:
        room = await get_or_create_dm(session, WA_BOT)
        if not room:
            raise HTTPException(500, "Could not reach WhatsApp bot")
        st["whatsapp"]["dm_room"]  = room
        st["whatsapp"]["status"]   = "waiting"
        st["whatsapp"]["qr"]       = None
        await send_text(session, room, "!wa login qr")
    return {"ok": True}


@app.post("/api/signal/link")
async def api_sig_link():
    if not st["token"]:
        raise HTTPException(503, "Not connected to Matrix yet")
    async with aiohttp.ClientSession() as session:
        room = await get_or_create_dm(session, SIG_BOT)
        if not room:
            raise HTTPException(500, "Could not reach Signal bot")
        st["signal"]["dm_room"]  = room
        st["signal"]["status"]   = "waiting"
        st["signal"]["qr"]       = None
        await send_text(session, room, "!signal login")
    return {"ok": True}


# ── Room / relay management ───────────────────────────────────────────────────

@app.post("/api/rooms/refresh")
async def api_rooms_refresh():
    _spawn(_refresh_rooms())
    return {"ok": True}


@app.get("/api/rooms")
async def api_rooms():
    return st["rooms"]


class RelayBody(BaseModel):
    signal_room:   str
    whatsapp_room: str
    name:          str = ""


@app.post("/api/relays")
async def api_relay_create(body: RelayBody):
    relays = load_relays()
    relay = {
        "id":            uuid.uuid4().hex[:8],
        "name":          body.name or "relay",
        "signal_room":   body.signal_room,
        "whatsapp_room": body.whatsapp_room,
    }
    relays.append(relay)
    save_relays(relays)
    _sse_push({"type": "relays_updated", "relays": relays})
    return relay


@app.delete("/api/relays/{relay_id}")
async def api_relay_delete(relay_id: str):
    relays = [r for r in load_relays() if r["id"] != relay_id]
    save_relays(relays)
    _sse_push({"type": "relays_updated", "relays": relays})
    return {"ok": True}

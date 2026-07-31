"""Watch-together real-time sync for Hub.

    POST /api/together/host                {item_id?, item_title?} -> creates a room (you become host)
    POST /api/together/join                {code}                  -> joins an existing room
    POST /api/together/leave               {code}                  -> leaves (host: ends) a room
    GET  /api/together/room/{code}         -> current room snapshot (reconnect / first paint)
    GET  /api/together/stream/{code}       -> SSE: live roster/playback/closed events for one room
    POST /api/together/room/{code}/command {action, position_ticks?} -> host-only playback command

Status (2026-07-23): fully wired end to end, both scaffolding passes are
done. host/join/leave/room/stream landed first; `command` (host-only
play/pause/seek broadcast) is now real too — static/index.html's
`_pvHostBroadcast()` calls it from the host's own `<video>` events, and
every participant's SSE stream applies incoming `playback` events to their
own player via `_pvApplyRemoteCommand()`. window._room's documented shape
grew two fields to carry this (`playback`, `runtimeMin`) — see that file's
comments for the exact contract. Joining a room now also auto-launches the
guest's own player onto the host's current item (their own
/api/playback/* stream — only the play/pause/seek *state* is synced, never
the media itself).

Post-deploy fix (2026-07-23, same day): a room's item_id used to be pinned
once at creation (from whatever window._currentItem was when hostRoom() was
called) and never revisited. Fine for a movie, where that id IS the
playable id — but for a series, window._currentItem is the *series*, while
the actual playing thing (and the id _pvHostBroadcast compares against) is
the *episode*, so nothing ever matched and series playback silently never
synced. Fixed by having `command` accept (and CommandBody.item_id, above,
carry) the real on-screen item on every call, updating room.item_id/
item_title/runtime_min in place rather than treating them as immutable
creation-time fields. This is also the durable answer to the
previously-flagged "what happens when the host switches titles mid-room"
question: it's no longer a special case, just another command whose
item_id differs from the room's last one.

── Registry: in-memory, not Postgres ────────────────────────────────────────
Rooms live in a module-level dict, not a table. Deliberate, not an oversight:
  - hub runs as a single container with uvicorn's default single worker (see
    hub/Dockerfile's CMD — no --workers, no compose `replicas:`), so
    "in-memory" here really does mean "one process, one source of truth," not
    "silently inconsistent across workers." If hub ever gets a second worker
    or a second replica this stops being true and the registry would need to
    move to Postgres (or Redis) — flagged, not assumed away.
  - Room/participant/playback-position churn is exactly the kind of
    high-frequency, fully-ephemeral state (a seek can fire every few seconds)
    that would otherwise turn into meaningful write amplification on the
    same Postgres box the nightly ETL and every other project router shares.
  - Trade-off accepted on purpose: a `docker compose up -d --build` redeploy
    (or a plain container restart) drops every open room. Treated the same
    way an in-progress Jellyfin transcode already is on redeploy — acceptable
    for a live watch-party, not something a durability guarantee is owed to.

── Identity & authorization ─────────────────────────────────────────────────
Every route requires a valid hub_session (auth.get_session_identity() — same
cookie playback.py already gates on). A room's own membership check is a
*second*, per-room gate on top of that: only /host and /join hand back a
room snapshot to someone who isn't already in it; /room and /stream both
403 a signed-in visitor who never joined that specific code, so a valid
session can't be used to enumerate or eavesdrop on someone else's room.
Playback commands are host-only for now (room.host_session_id check) — see
the module docstring above and the architecture writeup for why that's a
decision point, not a settled design.

── Transport: SSE, not WebSocket ────────────────────────────────────────────
Server -> browser push (roster changes, a future playback broadcast) goes
out over one long-lived GET (text/event-stream), matching the house
preference recorded in the modernization handoff's Stage 8 plan. Browser ->
server stays plain POST (host/join/leave/command) — there is no case here
that actually *needs* a single bidirectional socket, and SSE gets several
things for free that would otherwise be hand-rolled on a WebSocket: the
existing hub_session cookie rides along on the initial GET with no separate
handshake/auth-message step, EventSource reconnects on its own with backoff,
and it's plain HTTP/1.1 so no proxy `Upgrade` header wiring is required
before this can go through Nginx Proxy Manager in front of hub. (It DOES
need `X-Accel-Buffering: no`, set below, so a buffering proxy doesn't sit on
the stream — worth a live check the first time this deploys, not assumed.)
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from projects import prefs
from projects.auth import find_active_session_by_username, get_session_identity

router = APIRouter()

# no 0/O/1/I - same alphabet the old client-side mockRoomCode() used, kept
# for continuity (a code generated before this session's code and one
# generated after look identical to a user reading one aloud).
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LEN = 5
_MAX_PARTICIPANTS = 12          # arbitrary, generous-for-a-watch-party cap
# Comment-ping so idle proxies don't time the stream out - NOT the primary
# defense against buffering (that's proxy_buffering off + X-Accel-Buffering
# below, at the reverse proxy). Kept low (was 15s, prod-verified as the exact
# size of an observed sync delay - a buffering proxy flushes on ANY write,
# including this one, so a slow keepalive silently becomes the de facto sync
# latency ceiling) as defense-in-depth: even if a future proxy config
# regresses buffering, no one ever waits more than a few seconds either way.
_SSE_KEEPALIVE_SECS = 3
_SWEEP_INTERVAL_SECS = 60       # how often the idle-room sweep runs
_SWEEP_GRACE_SECS = 120         # room with 0 live subscribers this long -> reaped
_MAX_ROOM_AGE_SECS = 12 * 3600  # hard cap regardless of activity - safety net, not a UX target


@dataclass
class _Participant:
    session_id: str
    username: str
    is_host: bool
    joined_at: float = field(default_factory=time.time)


@dataclass
class _Room:
    code: str
    host_session_id: str
    item_id: str | None
    item_title: str | None
    # Approximate runtime (whole minutes, matching the card shape's own
    # runtime_min - not exact ticks) purely for the lobby's shared-player
    # status mirror to show a real duration/progress fraction instead of the
    # old permanent "--:--". Optional and best-effort: absent for anything
    # hosted before this field existed, or for an item with no known runtime.
    runtime_min: int | None = None
    participants: dict[str, _Participant] = field(default_factory=dict)  # session_id -> Participant
    playback: dict = field(default_factory=lambda: {
        "status": "paused", "position_ticks": 0, "updated_at": time.time(),
    })
    created_at: float = field(default_factory=time.time)
    subscribers: dict[str, asyncio.Queue] = field(default_factory=dict)  # session_id -> event queue
    # When this room last had zero live SSE subscribers, or None while it has
    # >=1. Seeded to "now" (== created_at) rather than None: a room that's
    # created but never actually gets an SSE connection opened against it
    # (tab closed before the EventSource fired) should still be sweepable via
    # the grace period, not only via the hard age cap.
    last_emptied_at: float | None = field(default_factory=time.time)


_rooms: dict[str, _Room] = {}
_lock = asyncio.Lock()  # guards _rooms + room.participants/subscribers mutation


def _new_code() -> str:
    for _ in range(20):  # generous retry budget; collision odds are astronomically low well before this
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
        if code not in _rooms:
            return code
    raise RuntimeError("could not allocate a unique room code")


def _snapshot(room: _Room, caller_session_id: str) -> dict:
    """Wire shape for a room, from one caller's point of view. `is_host`
    reflects *this caller's* role (frontend maps it straight onto
    window._room.isHost) — never includes anyone's session_id, only display
    usernames, since session_id is also the room's internal auth key."""
    return {
        "code": room.code,
        "is_host": room.host_session_id == caller_session_id,
        "item_id": room.item_id,
        "item_title": room.item_title,
        "runtime_min": room.runtime_min,
        "participants": [
            {"username": p.username, "is_host": p.is_host}
            for p in sorted(room.participants.values(), key=lambda p: p.joined_at)
        ],
        "playback": dict(room.playback),
    }


def _roster_payload(room: _Room) -> dict:
    """Broadcast-safe roster shape - unlike _snapshot(), this is the SAME
    payload for every subscriber, so it deliberately omits `is_host`/
    `item_id`/`item_title`: `is_host` in _snapshot() means "is *this caller*
    the host," which is meaningless (and actively wrong) once one payload
    goes out to N different recipients over the broadcast fan-out. Every
    caller already has item_id/item_title from their own /host or /join
    response and doesn't need it repeated on every roster tick."""
    return {
        "code": room.code,
        "participants": [
            {"username": p.username, "is_host": p.is_host}
            for p in sorted(room.participants.values(), key=lambda p: p.joined_at)
        ],
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _broadcast(room: _Room, event: str, data: dict) -> None:
    """Fan-out to every live subscriber queue for this room. Queues are
    unbounded (asyncio.Queue()), so put_nowait never blocks/raises here —
    a slow/stalled subscriber just accumulates backlog until it reconnects
    or gets swept, it never stalls the broadcaster."""
    for q in room.subscribers.values():
        q.put_nowait({"event": event, "data": data})


def _unauthorized() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)


class HostBody(BaseModel):
    item_id: str | None = None
    item_title: str | None = None
    runtime_min: int | None = None


class JoinBody(BaseModel):
    code: str


class HotJoinBody(BaseModel):
    """Hot-join from a Now Playing card - see /hot-join's docstring. Position/
    paused/runtime are whatever the caller's own Now Playing poll last
    observed for the target (client-supplied, same trust level as every
    other position value this module already accepts - see CommandBody)."""
    username: str
    item_id: str
    item_title: str | None = None
    position_ticks: int = 0
    paused: bool = False
    runtime_min: int | None = None


def _find_open_room(item_id: str, username: str) -> "_Room | None":
    """Hot-join's dedup check: is there already a room for this exact item,
    with this exact person (host or guest) already in it? If so, hot-join
    is just /join under a different name - resolves the "what if the
    watcher already has a room" question by making that the SAME room
    rather than spinning up a second, parallel one for the same watch.
    Linear scan, not indexed - together.py's room count is small enough
    (an in-memory dict for one process, see the module docstring) that this
    is cheaper to keep simple than to optimize."""
    for room in _rooms.values():
        if room.item_id != item_id:
            continue
        if any(p.username == username for p in room.participants.values()):
            return room
    return None


_COMMAND_ACTIONS = {"play", "pause", "seek"}


class CommandBody(BaseModel):
    action: str                    # "play" | "pause" | "seek"
    position_ticks: int = 0
    # The item actually on-screen in the host's player right now - sent on
    # every command (cheap, always known client-side), not just when it
    # changes. This is what makes a series' per-episode ids sync at all: the
    # room no longer pins one item_id at creation time and compares against
    # it forever (that was the bug - a series' room was created with the
    # *series* id, but the player's real id is the *episode*, so nothing
    # ever matched). Instead the room just tracks whatever the host is
    # actually watching, which also happens to be the durable answer to
    # "what happens when the host switches titles mid-room" flagged earlier
    # as an open gap - it's no longer a special case, it's just another
    # command carrying a different item_id than last time.
    item_id: str | None = None
    item_title: str | None = None
    runtime_min: int | None = None


@router.post("/host")
async def host_room(body: HostBody, request: Request):
    identity = await get_session_identity(request)
    if identity is None:
        return _unauthorized()
    session_id, username = identity

    async with _lock:
        code = _new_code()
        room = _Room(
            code=code, host_session_id=session_id,
            item_id=(body.item_id or None), item_title=(body.item_title or None),
            runtime_min=(body.runtime_min if body.runtime_min and body.runtime_min > 0 else None),
        )
        room.participants[session_id] = _Participant(session_id, username, is_host=True)
        _rooms[code] = room

    return {"ok": True, "room": _snapshot(room, session_id)}


@router.post("/join")
async def join_room(body: JoinBody, request: Request):
    identity = await get_session_identity(request)
    if identity is None:
        return _unauthorized()
    session_id, username = identity

    code = (body.code or "").strip().upper()
    if not code:
        return JSONResponse({"ok": False, "error": "A room code is required"}, status_code=400)

    async with _lock:
        room = _rooms.get(code)
        if room is None:
            return JSONResponse({"ok": False, "error": "Room not found"}, status_code=404)
        already_in = session_id in room.participants
        if not already_in and len(room.participants) >= _MAX_PARTICIPANTS:
            return JSONResponse({"ok": False, "error": "Room is full"}, status_code=409)
        room.participants[session_id] = _Participant(session_id, username, is_host=False)

    if not already_in:
        await _broadcast(room, "roster", _roster_payload(room))
    return {"ok": True, "room": _snapshot(room, session_id)}


@router.post("/hot-join")
async def hot_join(body: HotJoinBody, request: Request):
    """Join an in-progress solo watch straight from a Now Playing card - no
    invite code. Two paths, resolved by _find_open_room():
      1. A room already exists for that person+item (they're hosting, or
         already have guests) - this is just /join under another name.
      2. No room yet - the target is watching solo. Auto-creates one with
         THEM as host and seeds room.playback from what the caller observed
         on the card. This is the "does a solo watcher get auto-promoted to
         host" design question, answered: yes, silently and immediately
         (open-join, no approval round-trip - the trade-off for a fast,
         frictionless "hot" join) - but not invisibly. The target's own
         browser has done nothing and doesn't know this room exists yet; it
         discovers it (and shows them a toast, not a silent takeover) via
         GET /my-room, which its own already-running Now Playing poll
         checks passively - see static/index.html's checkMyRoom().
    Requires the target to actually be signed into Hub right now (a live
    hub_sessions row for that username, via find_active_session_by_username)
    - a Jellyfin session started outside Hub (the direct Jellyfin web app,
    say) has no hub session to promote to room host, and this fails
    gracefully rather than crash."""
    identity = await get_session_identity(request)
    if identity is None:
        return _unauthorized()
    session_id, username = identity

    target_username = (body.username or "").strip()
    if not target_username or not body.item_id:
        return JSONResponse({"ok": False, "error": "Missing target"}, status_code=400)
    if target_username == username:
        return JSONResponse({"ok": False, "error": "Can't watch with yourself"}, status_code=400)

    # The DB lookup (only on the create path, below) happens inside this
    # same lock rather than outside it - simpler and safer against two
    # simultaneous hot-joins of the same solo watcher racing to both decide
    # "no room exists yet" and create two - at the expense of briefly
    # blocking other room operations on a Postgres round-trip. Acceptable
    # at hub's realistic scale (a handful of hot-joins a day, not a second);
    # flagged rather than silently assumed fine forever.
    async with _lock:
        room = _find_open_room(body.item_id, target_username)
        if room is not None:
            already_in = session_id in room.participants
            if not already_in and len(room.participants) >= _MAX_PARTICIPANTS:
                return JSONResponse({"ok": False, "error": "Room is full"}, status_code=409)
            room.participants[session_id] = _Participant(session_id, username, is_host=False)
        else:
            # No room exists yet -> this would auto-promote a *solo* watcher to
            # host. That's exactly the path the privacy toggle gates: only do it
            # if they've opted in. now() already hides the CTA for opted-out
            # users, so a normal client never reaches here for them — this is
            # the defense-in-depth re-check against a hand-crafted request. (The
            # join-an-existing-room branch above is deliberately NOT gated:
            # hosting/accepting a room is itself the opt-in for that room.)
            if not await prefs.get_allow_hot_join(target_username):
                return JSONResponse(
                    {"ok": False, "error": "That viewer isn't available to watch with right now"},
                    status_code=404,
                )
            target = await find_active_session_by_username(target_username)
            if target is None:
                return JSONResponse(
                    {"ok": False, "error": "That viewer isn't available to watch with right now"},
                    status_code=404,
                )
            target_session_id, target_username_confirmed = target
            code = _new_code()
            room = _Room(
                code=code, host_session_id=target_session_id,
                item_id=body.item_id, item_title=body.item_title,
                runtime_min=(body.runtime_min if body.runtime_min and body.runtime_min > 0 else None),
            )
            room.playback = {
                "status": "paused" if body.paused else "playing",
                "position_ticks": max(0, body.position_ticks),
                "updated_at": time.time(),
            }
            room.participants[target_session_id] = _Participant(
                target_session_id, target_username_confirmed, is_host=True
            )
            room.participants[session_id] = _Participant(session_id, username, is_host=False)
            _rooms[code] = room
            already_in = False

    if not already_in:
        await _broadcast(room, "roster", _roster_payload(room))
    return {"ok": True, "room": _snapshot(room, session_id)}


@router.get("/my-room")
async def my_room(request: Request):
    """Passive discovery for someone who never clicked host/join themselves
    - specifically, the original solo watcher a hot-join above just
    auto-promoted to host. Polled by static/index.html's checkMyRoom(),
    which gates it to only run while the caller is actively playing
    something AND doesn't already know about a room, so this stays cheap -
    a small in-memory scan (not indexed, see _find_open_room's docstring for
    why that's fine here), not a poll every client makes constantly."""
    identity = await get_session_identity(request)
    if identity is None:
        return _unauthorized()
    session_id, _username = identity
    for room in _rooms.values():
        if session_id in room.participants:
            return {"ok": True, "room": _snapshot(room, session_id)}
    return {"ok": True, "room": None}


@router.post("/leave")
async def leave_room(body: JoinBody, request: Request):
    """Idempotent: leaving a room you're not in (already left, already
    swept, typo'd code) is a quiet no-op, not an error — the frontend calls
    this on both an explicit "Leave"/"End room" click and the `beforeunload`
    path, and neither should ever surface a scary failure to the user."""
    identity = await get_session_identity(request)
    if identity is None:
        return _unauthorized()
    session_id, _username = identity

    code = (body.code or "").strip().upper()
    room_gone = False
    async with _lock:
        room = _rooms.get(code)
        if room is None or session_id not in room.participants:
            return {"ok": True}
        was_host = room.host_session_id == session_id
        if was_host:
            # Hosting is what makes a room a room - it doesn't survive the
            # host explicitly ending it. (What happens when a host merely
            # *disconnects* without clicking "End room" is the idle-sweep's
            # job, not this handler's - see _sweep_rooms below.)
            del _rooms[code]
            room_gone = True
        else:
            del room.participants[session_id]

    if room_gone:
        await _broadcast(room, "closed", {"code": code})
    else:
        await _broadcast(room, "roster", _roster_payload(room))
    return {"ok": True}


@router.get("/room/{code}")
async def room_snapshot(code: str, request: Request):
    """Point-in-time fetch for reconnect / first paint before the SSE
    connection is up. 403s (not 404s) for a signed-in visitor who isn't a
    participant of this specific code, so a valid session alone can't be
    used to probe whether a code exists."""
    identity = await get_session_identity(request)
    if identity is None:
        return _unauthorized()
    session_id, _username = identity

    code = code.strip().upper()
    room = _rooms.get(code)
    if room is None or session_id not in room.participants:
        return JSONResponse({"ok": False, "error": "Not in this room"}, status_code=403)
    return {"ok": True, "room": _snapshot(room, session_id)}


@router.get("/stream/{code}")
async def stream(code: str, request: Request):
    """SSE stream for one room. Must already be a participant (call /join
    first) - this endpoint never adds you to a room itself, only subscribes
    an existing membership to live updates. Emits an immediate `snapshot`
    event so a client never has to race its own /join response against the
    stream's first push, then `roster`/`closed` (and, once wired,
    `playback`) events as they happen, with a comment-only keepalive line
    every _SSE_KEEPALIVE_SECS so idle proxies don't time the connection out."""
    identity = await get_session_identity(request)
    if identity is None:
        return _unauthorized()
    session_id, _username = identity

    code = code.strip().upper()
    room = _rooms.get(code)
    if room is None or session_id not in room.participants:
        return JSONResponse({"ok": False, "error": "Not in this room"}, status_code=403)

    queue: asyncio.Queue = asyncio.Queue()
    async with _lock:
        room.subscribers[session_id] = queue
        room.last_emptied_at = None  # at least one live listener again

    async def gen():
        try:
            yield _sse("snapshot", _snapshot(room, session_id))
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_SECS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(item["event"], item["data"])
                if item["event"] == "closed":
                    break
        finally:
            async with _lock:
                # Only drop *this* subscriber entry, and only if it's still
                # ours - /join can re-subscribe under the same session_id on
                # a fast reconnect, and this shouldn't race-delete that one.
                if room.subscribers.get(session_id) is queue:
                    del room.subscribers[session_id]
                    if not room.subscribers:
                        room.last_emptied_at = time.time()

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/room/{code}/command")
async def command(code: str, body: CommandBody, request: Request):
    """Host-only playback command broadcast. Live as of the sync-wiring pass:
    static/index.html's `_pvHostBroadcast()` calls this from the host's own
    `<video>` play/pause/seeked events, and every other participant's open
    SSE stream applies the resulting `playback` event to their own player
    (`_pvApplyRemoteCommand()`) — see that module's comments for the
    host-only-authorization / no-feedback-loop reasoning on the frontend
    side. `action` is validated against a fixed set now that this is a real,
    reachable-from-the-UI surface rather than curl-only scaffolding."""
    identity = await get_session_identity(request)
    if identity is None:
        return _unauthorized()
    session_id, _username = identity

    if body.action not in _COMMAND_ACTIONS:
        return JSONResponse({"ok": False, "error": "Unknown action"}, status_code=400)

    code = code.strip().upper()
    room = _rooms.get(code)
    if room is None or session_id not in room.participants:
        return JSONResponse({"ok": False, "error": "Not in this room"}, status_code=403)
    if room.host_session_id != session_id:
        return JSONResponse({"ok": False, "error": "Only the host can control playback"}, status_code=403)

    room.playback = {
        "status": "playing" if body.action == "play" else "paused" if body.action == "pause" else room.playback["status"],
        "position_ticks": body.position_ticks,
        "updated_at": time.time(),
    }
    # Keep the room's item pinned to whatever's actually on-screen for the
    # host (see CommandBody.item_id's docstring) - only overwrite when a
    # value was actually sent, so an older/partial client that only ever
    # posts {action, position_ticks} can't accidentally null these out.
    if body.item_id:
        room.item_id = body.item_id
        room.item_title = body.item_title or room.item_title
        room.runtime_min = body.runtime_min if body.runtime_min and body.runtime_min > 0 else room.runtime_min

    await _broadcast(room, "playback", {
        "action": body.action, **room.playback,
        "item_id": room.item_id, "item_title": room.item_title, "runtime_min": room.runtime_min,
    })
    return {"ok": True}


async def sweep_idle_rooms() -> None:
    """Background loop (started alongside stats.status_sampler() in app.py's
    lifespan): reaps rooms nobody's listening to anymore, and hard-caps room
    age regardless of activity, so a long-running hub process can't
    accumulate abandoned rooms forever. A room with subscribers is never
    touched here, no matter its age, short of the hard cap - normal
    long-running watch parties are the whole point of this feature."""
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL_SECS)
        now = time.time()
        async with _lock:
            dead = [
                (code, room) for code, room in _rooms.items()
                if (room.last_emptied_at is not None
                    and now - room.last_emptied_at > _SWEEP_GRACE_SECS)
                or (now - room.created_at > _MAX_ROOM_AGE_SECS)
            ]
            for code, _room in dead:
                del _rooms[code]
        # Broadcast outside the lock (best-effort; a room with 0 subscribers
        # almost always has 0 listeners left for this anyway).
        for code, room in dead:
            await _broadcast(room, "closed", {"code": code})

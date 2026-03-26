"""
Socket.IO server for real-time event delivery to frontend clients.

The frontend connects via socket.io-client to /ws with auth: { token }.
Events are emitted on the "event" channel as { type: ..., data: {...} }.

Option Chain WebSocket:
  - Clients emit "subscribe_chain" { underlying, expiry } to join a room.
  - Clients emit "unsubscribe_chain" to leave their current chain room.
  - A background task polls Dhan every 3s for each active room and broadcasts.
  - Room format: "chain:{UNDERLYING}:{EXPIRY}" — only rooms with >0 subscribers.
  - Room subscriber count is tracked via an atomic counter (no dict-drift risk).
"""

import asyncio
import os
from collections import defaultdict

import jwt
import socketio
import structlog

logger = structlog.get_logger(service="dashboard_api", component="socketio")

JWT_SECRET = os.environ.get("JWT_SECRET", "")
JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    # ping_timeout / ping_interval keep idle connections alive and detect
    # dead clients quickly so rooms don't accumulate ghost subscribers.
    ping_timeout=20,
    ping_interval=25,
)


def create_sio_app(other_app=None):
    """Create the Socket.IO ASGI app, optionally wrapping another ASGI app."""
    return socketio.ASGIApp(sio, other_app, socketio_path="/socket.io")


# Mapping: tenant_id -> set of sids
_tenant_sids: dict[str, set[str]] = defaultdict(set)

# Chain room subscriber counts — the single source of truth for "is room active?"
# Using a counter instead of a set-of-sids avoids drift between our dict and
# Socket.IO's internal room state under heavy connect/disconnect churn.
_chain_room_refcount: dict[str, int] = defaultdict(int)

# Background chain broadcaster task handle
_chain_broadcaster_task: asyncio.Task | None = None


# ── Authentication ──────────────────────────────────────────────────────────


@sio.event
async def connect(sid, environ, auth):
    """Authenticate the socket connection using JWT from auth.token."""
    if not auth or not isinstance(auth, dict) or "token" not in auth:
        logger.warning("socketio_connect_rejected", sid=sid, reason="missing_token")
        raise socketio.exceptions.ConnectionRefusedError("Authentication required")

    token = auth["token"]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        tenant_id = payload.get("sub")
        if not tenant_id:
            raise ValueError("Missing sub claim")
    except (jwt.InvalidTokenError, ValueError) as exc:
        logger.warning("socketio_connect_rejected", sid=sid, reason=str(exc))
        raise socketio.exceptions.ConnectionRefusedError("Invalid token")

    await sio.save_session(sid, {"tenant_id": tenant_id, "chain_room": None})
    _tenant_sids[tenant_id].add(sid)

    logger.info("socketio_connected", sid=sid, tenant_id=tenant_id)


@sio.event
async def disconnect(sid):
    """Clean up tenant tracking and chain room on disconnect."""
    session = await sio.get_session(sid)
    tenant_id = session.get("tenant_id") if session else None

    if tenant_id and tenant_id in _tenant_sids:
        _tenant_sids[tenant_id].discard(sid)
        if not _tenant_sids[tenant_id]:
            del _tenant_sids[tenant_id]

    # Decrement chain room refcount
    chain_room = session.get("chain_room") if session else None
    if chain_room:
        _decrement_room(chain_room)

    logger.info("socketio_disconnected", sid=sid, tenant_id=tenant_id)


# ── Option Chain Subscription ───────────────────────────────────────────────


def _decrement_room(room: str) -> None:
    """Safely decrement a room's refcount, cleaning up at zero."""
    if room in _chain_room_refcount:
        _chain_room_refcount[room] = max(0, _chain_room_refcount[room] - 1)
        if _chain_room_refcount[room] == 0:
            del _chain_room_refcount[room]


@sio.event
async def subscribe_chain(sid, data):
    """
    Client subscribes to option chain updates.

    Expects: { "underlying": "NIFTY", "expiry": "2026-04-02" }
    Joins room "chain:NIFTY:2026-04-02" and leaves any previous chain room.
    """
    if not isinstance(data, dict):
        return

    underlying = (data.get("underlying") or "").upper()
    expiry = data.get("expiry") or ""

    if not underlying or not expiry:
        return

    session = await sio.get_session(sid)
    if not session:
        return

    room = f"chain:{underlying}:{expiry}"

    # Leave previous chain room if different
    prev_room = session.get("chain_room")
    if prev_room == room:
        return  # Already in this room — no-op
    if prev_room:
        _decrement_room(prev_room)
        sio.leave_room(sid, prev_room)

    # Join new room
    sio.enter_room(sid, room)
    _chain_room_refcount[room] += 1
    session["chain_room"] = room
    await sio.save_session(sid, session)

    logger.debug(
        "chain_subscribed",
        sid=sid,
        room=room,
        room_size=_chain_room_refcount.get(room, 0),
    )


@sio.event
async def unsubscribe_chain(sid, data=None):
    """Client unsubscribes from option chain updates."""
    session = await sio.get_session(sid)
    if not session:
        return

    room = session.get("chain_room")
    if room:
        _decrement_room(room)
        sio.leave_room(sid, room)
        session["chain_room"] = None
        await sio.save_session(sid, session)


# ── Background Chain Broadcaster ────────────────────────────────────────────


async def _fetch_and_broadcast(redis, room: str, underlying: str, expiry: str, mapping: dict) -> None:
    """Fetch one room's chain data and emit to the room."""
    from .routers.chain import _cached_chain

    result = await _cached_chain(redis, underlying, expiry, mapping)

    payload = {
        "type": "OPTION_CHAIN",
        "data": {
            "underlying": underlying,
            "expiry": expiry,
            **result,
        },
    }

    await sio.emit("event", payload, room=room)


async def _chain_broadcast_loop(redis):
    """
    Background loop: every 3s, for each active chain room, fetch from
    Redis cache (hits Dhan only on cache miss) and broadcast.

    All active rooms are fetched CONCURRENTLY via asyncio.gather so 5
    underlyings don't take 5 * 3s = 15s sequentially.
    """
    from .routers.chain import UNDERLYING_MAP

    logger.info("chain_broadcaster_started")

    while True:
        try:
            await asyncio.sleep(3)

            # Snapshot active rooms
            active_rooms = [
                room for room, count in _chain_room_refcount.items() if count > 0
            ]
            if not active_rooms:
                continue

            tasks = []
            for room in active_rooms:
                parts = room.split(":", 2)
                if len(parts) != 3:
                    continue
                _, underlying, expiry = parts

                if underlying not in UNDERLYING_MAP:
                    continue

                tasks.append(
                    _fetch_and_broadcast(redis, room, underlying, expiry, UNDERLYING_MAP[underlying])
                )

            # Fan-out: all rooms fetched concurrently
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.warning(
                        "chain_broadcast_error",
                        room=active_rooms[i] if i < len(active_rooms) else "unknown",
                        error=str(result),
                    )

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("chain_broadcast_loop_error", error=str(exc))
            await asyncio.sleep(1)

    logger.info("chain_broadcaster_stopped")


def start_chain_broadcaster(redis) -> asyncio.Task:
    """Start the background chain broadcaster. Call once during app startup."""
    global _chain_broadcaster_task
    _chain_broadcaster_task = asyncio.create_task(_chain_broadcast_loop(redis))
    return _chain_broadcaster_task


def stop_chain_broadcaster():
    """Cancel the background chain broadcaster."""
    global _chain_broadcaster_task
    if _chain_broadcaster_task and not _chain_broadcaster_task.done():
        _chain_broadcaster_task.cancel()


def get_active_chain_rooms() -> dict[str, int]:
    """Return active chain rooms with subscriber counts (for monitoring)."""
    return {room: count for room, count in _chain_room_refcount.items() if count > 0}


# ── Tenant Emit ─────────────────────────────────────────────────────────────


async def emit_to_tenant(tenant_id: str, event_type: str, data: dict) -> None:
    """Emit an event to all connected sockets for a given tenant."""
    sids = _tenant_sids.get(tenant_id, set())
    payload = {"type": event_type, "data": data}
    for sid in list(sids):
        try:
            await sio.emit("event", payload, to=sid)
        except Exception:
            logger.warning(
                "socketio_emit_failed",
                sid=sid,
                tenant_id=tenant_id,
                event_type=event_type,
            )

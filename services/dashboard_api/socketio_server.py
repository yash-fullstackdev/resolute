"""
Socket.IO server for real-time event delivery to frontend clients.

The frontend connects via socket.io-client to /ws with auth: { token }.
Events are emitted on the "event" channel as { type: ..., data: {...} }.
"""

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
)

def create_sio_app(other_app=None):
    """Create the Socket.IO ASGI app, optionally wrapping another ASGI app."""
    return socketio.ASGIApp(sio, other_app, socketio_path="/socket.io")

# Mapping: tenant_id -> set of sids
_tenant_sids: dict[str, set[str]] = defaultdict(set)


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

    await sio.save_session(sid, {"tenant_id": tenant_id})
    _tenant_sids[tenant_id].add(sid)

    logger.info("socketio_connected", sid=sid, tenant_id=tenant_id)


@sio.event
async def disconnect(sid):
    """Clean up tenant tracking on disconnect."""
    session = await sio.get_session(sid)
    tenant_id = session.get("tenant_id") if session else None

    if tenant_id and tenant_id in _tenant_sids:
        _tenant_sids[tenant_id].discard(sid)
        if not _tenant_sids[tenant_id]:
            del _tenant_sids[tenant_id]

    logger.info("socketio_disconnected", sid=sid, tenant_id=tenant_id)


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

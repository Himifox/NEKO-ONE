"""Versioned multi-visitor WebSocket protocol for the public room."""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from main_logic.room.models import utc_now
from main_logic.room.service import RoomInputError
from main_logic.room.session import COOKIE_NAME

router = APIRouter()


def _origin_allowed(websocket: WebSocket) -> bool:
    origin = (websocket.headers.get("origin") or "").strip().rstrip("/")
    configured = {
        value.strip().rstrip("/")
        for value in os.environ.get(
            "NEKO_PUBLIC_ALLOWED_ORIGINS",
            "http://127.0.0.1:48911,http://localhost:48911",
        ).split(",")
        if value.strip()
    }
    if origin:
        return origin in configured
    return os.environ.get("NEKO_PUBLIC_ALLOW_MISSING_ORIGIN", "1") == "1"


@router.websocket("/ws/rooms/{room_id}")
async def public_room_websocket(websocket: WebSocket, room_id: str) -> None:
    if room_id != "main":
        await websocket.close(code=4404, reason="room_not_found")
        return
    if not _origin_allowed(websocket):
        await websocket.close(code=4403, reason="origin_not_allowed")
        return

    service = websocket.app.state.room_service
    sessions = websocket.app.state.guest_sessions
    visitor = await sessions.resolve(websocket.cookies.get(COOKIE_NAME))
    if visitor is None:
        await websocket.close(code=4401, reason="guest_session_required")
        return

    try:
        after_seq = max(0, int(websocket.query_params.get("after_seq", "0")))
    except ValueError:
        await websocket.close(code=4400, reason="invalid_after_seq")
        return

    await websocket.accept()
    connection = await service.hub.register(
        websocket, room_id=room_id, visitor_id=visitor.id
    )
    try:
        room = await service.store.room_snapshot(room_id)
        await service.hub.send(
            connection,
            {
                "type": "session.ready",
                "protocol_version": 1,
                "connection_id": connection.id,
                "room_id": room_id,
                "visitor": {
                    "id": visitor.id,
                    "display_name": visitor.display_name,
                },
                "last_room_seq": room["last_seq"],
                "oldest_available_seq": room["oldest_available_seq"],
                "heartbeat_interval_ms": 25000,
                "server_time": utc_now(),
            },
        )
        replay_from = max(0, room["oldest_available_seq"] - 1)
        if after_seq < replay_from or after_seq > room["last_seq"]:
            await service.hub.send(
                connection,
                {
                    "type": "replay.reset",
                    "server_time": utc_now(),
                    "payload": {
                        "reason": (
                            "history_expired"
                            if after_seq < replay_from
                            else "sequence_ahead"
                        ),
                        "requested_after_seq": after_seq,
                        "replay_from_seq": replay_from,
                        "last_room_seq": room["last_seq"],
                    },
                },
            )
            after_seq = replay_from
        missed = await service.store.list_events(room_id, after_seq, limit=1000)
        for event in missed:
            await service.hub.send(connection, event)
        active = service.active_generation(room_id)
        if active is not None:
            await service.hub.send(
                connection,
                {
                    "type": "stream.snapshot",
                    "server_time": utc_now(),
                    "payload": active.snapshot(),
                },
            )
        await service.hub.broadcast(room_id, await service.presence_event(room_id))

        while True:
            raw = await websocket.receive_text()
            if len(raw) > 8192:
                await service.hub.send(
                    connection,
                    {
                        "type": "command.rejected",
                        "server_time": utc_now(),
                        "payload": {"code": "frame_too_large"},
                    },
                )
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await service.hub.send(
                    connection,
                    {
                        "type": "command.rejected",
                        "server_time": utc_now(),
                        "payload": {"code": "invalid_json"},
                    },
                )
                continue
            message_type = message.get("type") if isinstance(message, dict) else None
            if message_type == "ping":
                await service.hub.send(
                    connection, {"type": "pong", "server_time": utc_now()}
                )
                continue
            if message_type != "chat.send":
                await service.hub.send(
                    connection,
                    {
                        "type": "command.rejected",
                        "server_time": utc_now(),
                        "payload": {"code": "unknown_command"},
                    },
                )
                continue
            payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
            try:
                accepted = await service.submit_message(
                    room_id=room_id,
                    visitor=visitor,
                    request_id=str(message.get("request_id") or ""),
                    text=str(payload.get("text") or ""),
                    reply_to_id=(str(payload["reply_to_id"]) if payload.get("reply_to_id") else None),
                )
                await service.hub.send(connection, accepted)
            except RoomInputError as exc:
                await service.hub.send(
                    connection,
                    {
                        "type": "command.rejected",
                        "request_id": message.get("request_id"),
                        "server_time": utc_now(),
                        "payload": {"code": exc.code, "message": str(exc)},
                    },
                )
    except WebSocketDisconnect:
        pass
    finally:
        await service.hub.unregister(connection)
        await service.hub.broadcast(room_id, await service.presence_event(room_id))

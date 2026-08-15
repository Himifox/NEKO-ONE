"""Minimal same-origin HTTP API for the public-room page."""

from __future__ import annotations

import os
import asyncio

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from main_logic.room.models import utc_now
from main_logic.room.session import COOKIE_NAME

router = APIRouter(prefix="/api/v1")


class GuestSessionRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=40)


def _service(request: Request):
    return request.app.state.room_service


def _sessions(request: Request):
    return request.app.state.guest_sessions


@router.get("/health/live")
async def health_live() -> dict:
    return {"ok": True, "service": "neko-public-room", "time": utc_now()}


@router.get("/health/ready")
async def health_ready(request: Request):
    service = _service(request)
    minimum_free_mib = service._env_int(
        "NEKO_PUBLIC_MIN_FREE_MIB", 256, 16, 102400
    )
    storage, conversation, memory = await asyncio.gather(
        service.store.readiness_snapshot(minimum_free_mib=minimum_free_mib),
        service.engine.readiness_snapshot(),
        service.memory.health_snapshot(),
    )
    room = await service.store.room_snapshot("main")
    ready = bool(
        storage.get("ok")
        and conversation.get("configured")
        and room.get("status") != "missing"
    )
    payload = {
        "ok": ready,
        "storage": storage,
        "conversation": conversation,
        "memory": memory,
        "optional": {
            "tts": service.speech.configured,
            "live2d": request.app.state.public_avatar.manifest()["enabled"],
        },
        "room": room,
        "time": utc_now(),
    }
    if not ready:
        return JSONResponse(status_code=503, content=payload)
    return payload


@router.post("/session/guest")
async def create_or_restore_guest(
    payload: GuestSessionRequest,
    request: Request,
    response: Response,
) -> dict:
    sessions = _sessions(request)
    visitor = await sessions.resolve(request.cookies.get(COOKIE_NAME))
    if visitor is None:
        requested = (payload.display_name or "").strip()
        if not requested:
            requested = f"Guest {os.urandom(2).hex().upper()}"
        visitor = await _service(request).store.create_visitor(requested)
    token = sessions.issue(visitor)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=sessions.max_age_seconds,
        httponly=True,
        secure=request.url.scheme == "https" or os.environ.get("NEKO_PUBLIC_SECURE_COOKIE") == "1",
        samesite="lax",
        path="/",
    )
    return {
        "visitor": {"id": visitor.id, "display_name": visitor.display_name},
        "server_time": utc_now(),
    }


@router.get("/rooms/{room_id}")
async def get_room(room_id: str, request: Request) -> dict:
    if room_id != "main":
        raise HTTPException(status_code=404, detail="room not found")
    snapshot = await _service(request).store.room_snapshot(room_id)
    snapshot["online"] = await _service(request).hub.online_count(room_id)
    snapshot["limits"] = dict(_service(request).limits)
    snapshot["controls"] = dict(_service(request).controls)
    avatar = request.app.state.public_avatar.manifest()
    snapshot["features"] = {
        "text": True,
        "live2d": avatar["enabled"],
        "tts": _service(request).speech.configured,
        "accounts": False,
    }
    return snapshot


@router.get("/avatar")
async def get_public_avatar(request: Request) -> dict:
    return request.app.state.public_avatar.manifest()


@router.get("/rooms/{room_id}/messages")
async def get_messages(
    room_id: str,
    request: Request,
    before_seq: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict:
    if room_id != "main":
        raise HTTPException(status_code=404, detail="room not found")
    messages = await _service(request).store.list_messages(
        room_id, before_seq=before_seq, limit=limit
    )
    return {"messages": [message.as_payload() for message in messages]}

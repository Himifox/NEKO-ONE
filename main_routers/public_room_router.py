"""Minimal same-origin HTTP API for the public-room page."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Query, Request, Response
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
async def health_ready(request: Request) -> dict:
    service = _service(request)
    snapshot = await service.store.room_snapshot("main")
    ready = snapshot.get("status") != "missing"
    if not ready:
        raise HTTPException(status_code=503, detail="room store unavailable")
    return {"ok": True, "room": snapshot, "time": utc_now()}


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
    snapshot["features"] = {
        "text": True,
        "live2d": True,
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

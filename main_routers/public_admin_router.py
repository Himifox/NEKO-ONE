"""Authenticated administration API for the public-room first release."""

from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from main_logic.room.admin_auth import ADMIN_COOKIE_NAME
from utils.config_manager import get_config_manager, get_reserved, set_reserved

router = APIRouter(prefix="/api/v1/admin")


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class PersonaUpdate(BaseModel):
    system_prompt: str = Field(min_length=1, max_length=50000)


class StatusUpdate(BaseModel):
    status: str


class RoomFactRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    importance: int = Field(default=5, ge=1, le=10)


class LimitsUpdate(BaseModel):
    max_message_chars: int = Field(ge=100, le=4000)
    messages_per_window: int = Field(ge=1, le=20)
    window_seconds: float = Field(ge=1, le=300)


class RoomControlsUpdate(BaseModel):
    paused: bool
    read_only: bool
    proactive_enabled: bool


def _auth(request: Request, *, write: bool = False) -> str:
    manager = request.app.state.admin_sessions
    if not manager.enabled:
        raise HTTPException(status_code=404, detail="admin is disabled")
    csrf = manager.resolve(request.cookies.get(ADMIN_COOKIE_NAME))
    if csrf is None:
        raise HTTPException(status_code=401, detail="admin session required")
    if write and not hmac.compare_digest(
        request.headers.get("x-neko-csrf", ""), csrf
    ):
        raise HTTPException(status_code=403, detail="invalid csrf token")
    return csrf


@router.post("/session")
async def login(payload: LoginRequest, request: Request, response: Response) -> dict:
    manager = request.app.state.admin_sessions
    if not manager.enabled:
        raise HTTPException(status_code=404, detail="admin is disabled")
    remote = request.client.host if request.client else "unknown"
    result = manager.authenticate(payload.password, remote)
    if result is None:
        raise HTTPException(status_code=401, detail="invalid credentials or rate limited")
    token, csrf = result
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        token,
        max_age=manager.max_age_seconds,
        httponly=True,
        secure=request.url.scheme == "https" or os.environ.get("NEKO_PUBLIC_SECURE_COOKIE") == "1",
        samesite="strict",
        path="/api/v1/admin",
    )
    return {"ok": True, "csrf": csrf}


@router.get("/session")
async def restore_session(request: Request) -> dict:
    return {"ok": True, "csrf": _auth(request)}


@router.delete("/session")
async def logout(request: Request, response: Response) -> dict:
    _auth(request, write=True)
    response.delete_cookie(ADMIN_COOKIE_NAME, path="/api/v1/admin")
    return {"ok": True}


async def _persona() -> tuple[str, str]:
    manager = get_config_manager()
    characters = await manager.aload_characters()
    current = characters.get("当前猫娘") or next(iter(characters.get("猫娘", {})), "")
    data = characters.get("猫娘", {}).get(current, {})
    return current, str(
        get_reserved(data, "system_prompt", default="", legacy_keys=("system_prompt",)) or ""
    )


@router.get("/state")
async def state(request: Request) -> dict:
    _auth(request)
    snapshot = await request.app.state.room_service.store.admin_snapshot()
    character, persona = await _persona()
    service = request.app.state.room_service
    active_generation = service.active_generation("main")
    snapshot.update(
        {
            "character": character,
            "persona": persona,
            "online": await service.hub.online_count("main"),
            "tts_configured": service.speech.configured,
            "limits": dict(service.limits),
            "controls": dict(service.controls),
            "active_generation": (
                active_generation.snapshot() if active_generation else None
            ),
        }
    )
    return snapshot


@router.put("/persona")
async def update_persona(payload: PersonaUpdate, request: Request) -> dict:
    _auth(request, write=True)
    manager = get_config_manager()
    characters = await manager.aload_characters()
    current = characters.get("当前猫娘") or next(iter(characters.get("猫娘", {})), "")
    if not current or current not in characters.get("猫娘", {}):
        raise HTTPException(status_code=409, detail="current character is missing")
    set_reserved(characters["猫娘"][current], "system_prompt", payload.system_prompt.strip())
    await manager.asave_characters(characters)
    await request.app.state.room_service.store.audit(
        "persona.update", "character", current, {"length": len(payload.system_prompt)}
    )
    return {"ok": True, "character": current}


@router.put("/limits")
async def update_limits(payload: LimitsUpdate, request: Request) -> dict:
    _auth(request, write=True)
    limits = await request.app.state.room_service.update_limits(payload.model_dump())
    return {"ok": True, "limits": limits}


@router.put("/room-controls")
async def update_room_controls(
    payload: RoomControlsUpdate, request: Request
) -> dict:
    _auth(request, write=True)
    controls = await request.app.state.room_service.update_controls(
        "main", payload.model_dump()
    )
    return {"ok": True, "controls": controls}


@router.post("/generation/cancel")
async def cancel_generation(request: Request) -> dict:
    _auth(request, write=True)
    cancelled = await request.app.state.room_service.cancel_generation("main")
    return {"ok": True, "cancelled": cancelled}


@router.put("/visitors/{visitor_id}/status")
async def visitor_status(visitor_id: str, payload: StatusUpdate, request: Request) -> dict:
    _auth(request, write=True)
    if payload.status not in {"active", "banned"}:
        raise HTTPException(status_code=400, detail="invalid visitor status")
    changed = await request.app.state.room_service.store.set_visitor_status(
        visitor_id, payload.status
    )
    if not changed:
        raise HTTPException(status_code=404, detail="visitor not found")
    if payload.status == "banned":
        await request.app.state.room_service.hub.disconnect_visitor(visitor_id)
    return {"ok": True, "status": payload.status}


@router.put("/messages/{message_id}/status")
async def message_status(message_id: str, payload: StatusUpdate, request: Request) -> dict:
    _auth(request, write=True)
    if payload.status not in {"visible", "hidden"}:
        raise HTTPException(status_code=400, detail="invalid message status")
    event = await request.app.state.room_service.store.moderate_message(
        message_id, payload.status
    )
    if event is None:
        raise HTTPException(status_code=404, detail="message not found")
    await request.app.state.room_service.hub.broadcast("main", event)
    return {"ok": True, "status": payload.status}


@router.post("/memory/room-facts")
async def add_room_fact(payload: RoomFactRequest, request: Request) -> dict:
    _auth(request, write=True)
    service = request.app.state.room_service
    character, _ = await service.engine.character()
    result = await service.memory.add_reviewed_room_fact(
        character_name=character,
        room_id="main",
        text=payload.text.strip(),
        importance=payload.importance,
    )
    await service.store.audit(
        "memory.room_fact.add", "room", "main", {"importance": payload.importance}
    )
    return {"ok": True, "result": result}


@router.delete("/memory/visitors/{visitor_id}")
async def forget_visitor(visitor_id: str, request: Request) -> dict:
    _auth(request, write=True)
    service = request.app.state.room_service
    character, _ = await service.engine.character()
    result = await service.memory.forget_visitor(
        character_name=character, room_id="main", visitor_id=visitor_id
    )
    await service.store.audit("memory.visitor.forget", "visitor", visitor_id)
    return {"ok": True, "result": result}

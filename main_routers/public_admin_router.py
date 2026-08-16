"""Authenticated administration API for the public-room first release."""

from __future__ import annotations

import hmac
import os

from config.prompts.prompts_chara import is_default_prompt
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from main_logic.room.admin_auth import ADMIN_COOKIE_NAME
from main_logic.room.conversation import MAX_PUBLIC_PERSONA_CHARS
from utils.config_manager import get_config_manager, get_reserved, set_reserved

router = APIRouter(prefix="/api/v1/admin")


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class PersonaUpdate(BaseModel):
    system_prompt: str = Field(min_length=1, max_length=MAX_PUBLIC_PERSONA_CHARS)


class CharacterSelectionUpdate(BaseModel):
    character: str = Field(min_length=1, max_length=50)


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


class AvatarUpdate(BaseModel):
    enabled: bool = True
    model_name: str | None = Field(default=None, max_length=128)
    model_file: str | None = Field(default=None, max_length=128)


class RetentionUpdate(BaseModel):
    message_days: int = Field(ge=1, le=3650)
    visitor_days: int = Field(ge=1, le=3650)
    audit_days: int = Field(ge=7, le=3650)
    speech_hours: int = Field(ge=1, le=8760)
    cleanup_interval_minutes: int = Field(ge=5, le=1440)


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


async def _persona(service) -> tuple[str, str, str, list[dict[str, str]]]:
    manager = get_config_manager()
    characters = await manager.aload_characters()
    current = characters.get("当前猫娘") or next(iter(characters.get("猫娘", {})), "")
    data = characters.get("猫娘", {}).get(current, {})
    stored = str(
        get_reserved(data, "system_prompt", default="", legacy_keys=("system_prompt",)) or ""
    )
    _runtime_character, effective = await service.engine.character()
    source = "builtin_default" if not stored or is_default_prompt(stored) else "custom"
    options: list[dict[str, str]] = []
    for name in characters.get("猫娘", {}):
        label = name
        if name.casefold() == "test":
            label = "Lanlan - 旧默认档案"
        options.append({"id": name, "label": label})
    return current, effective, source, options


@router.get("/state")
async def state(request: Request) -> dict:
    _auth(request)
    snapshot = await request.app.state.room_service.store.admin_snapshot()
    service = request.app.state.room_service
    active_character, persona, persona_source, character_options = await _persona(service)
    character, _prompt = await service.engine.character()
    active_generation = service.active_generation("main")
    snapshot.update(
        {
            "character": character,
            "active_character": active_character,
            "character_options": character_options,
            "persona": persona,
            "persona_source": persona_source,
            "online": await service.hub.online_count("main"),
            "tts_configured": service.speech.configured,
            "limits": dict(service.limits),
            "controls": dict(service.controls),
            "retention": dict(service.retention),
            "last_cleanup": service.last_cleanup,
            "dependencies": service.dependency_snapshot(),
            "active_generation": (
                active_generation.snapshot() if active_generation else None
            ),
            "avatar": {
                "current": request.app.state.public_avatar.manifest(),
                "models": request.app.state.public_avatar.installed_models(),
                "management_available": True,
            },
        }
    )
    return snapshot


@router.put("/avatar")
async def update_avatar(payload: AvatarUpdate, request: Request) -> dict:
    _auth(request, write=True)
    avatar = request.app.state.public_avatar
    previous = {
        "enabled": avatar.manifest()["enabled"],
        "model_name": avatar.model_name,
        "model_file": avatar.model_file,
    }
    if payload.enabled:
        try:
            manifest = avatar.configure(
                model_name=payload.model_name or "",
                model_file=payload.model_file or "",
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"model cannot be activated: {exc}"
            ) from exc
        selection = {
            "enabled": True,
            "model_name": avatar.model_name,
            "model_file": avatar.model_file,
        }
    else:
        manifest = avatar.disable()
        selection = {"enabled": False}
    try:
        await request.app.state.room_service.store.set_setting(
            "live2d_model", selection, actor_id="admin"
        )
    except Exception:
        avatar.restore(previous)
        request.app.state.live2d_static.replace_allowed_paths(
            avatar.public_asset_paths()
        )
        raise
    request.app.state.live2d_static.replace_allowed_paths(avatar.public_asset_paths())
    return {
        "ok": True,
        "current": manifest,
        "models": avatar.installed_models(),
    }


@router.put("/character")
async def update_character(payload: CharacterSelectionUpdate, request: Request) -> dict:
    _auth(request, write=True)
    selected = payload.character.strip()
    if not selected:
        raise HTTPException(status_code=422, detail="invalid character")
    manager = get_config_manager()
    characters = await manager.aload_characters()
    if selected not in characters.get("猫娘", {}):
        raise HTTPException(status_code=404, detail="character is not installed")
    characters["当前猫娘"] = selected
    await manager.asave_characters(characters)
    character = await request.app.state.room_service.refresh_character_identity()
    await request.app.state.room_service.store.audit(
        "character.select", "character", selected, {"display_name": character}
    )
    return {"ok": True, "character": character}


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


@router.put("/retention")
async def update_retention(payload: RetentionUpdate, request: Request) -> dict:
    _auth(request, write=True)
    retention = await request.app.state.room_service.update_retention(
        payload.model_dump()
    )
    return {"ok": True, "retention": retention}


@router.post("/retention/run")
async def run_retention(request: Request) -> dict:
    _auth(request, write=True)
    result = await request.app.state.room_service.run_retention_cleanup(
        actor_id="admin"
    )
    return {"ok": True, "result": result}


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
    event = await request.app.state.room_service.moderate_message(
        "main", message_id, payload.status
    )
    if event is None:
        raise HTTPException(status_code=404, detail="message not found")
    return {"ok": True, "status": payload.status}


@router.post("/memory/room-facts")
async def add_room_fact(payload: RoomFactRequest, request: Request) -> dict:
    _auth(request, write=True)
    service = request.app.state.room_service
    character = await service.engine.memory_character_name()
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
    character = await service.engine.memory_character_name()
    result = await service.memory.forget_visitor(
        character_name=character, room_id="main", visitor_id=visitor_id
    )
    await service.store.audit("memory.visitor.forget", "visitor", visitor_id)
    return {"ok": True, "result": result}

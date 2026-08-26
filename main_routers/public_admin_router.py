"""Authenticated administration API for the public-room first release."""

from __future__ import annotations

import asyncio
import hmac
import os

from config.prompts.prompts_chara import is_default_prompt
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from main_logic.room.admin_auth import ADMIN_COOKIE_NAME
from main_logic.room.conversation import MAX_PUBLIC_PERSONA_CHARS
from utils.api_config_loader import (
    get_assist_api_providers_for_frontend,
    get_cosyvoice_clone_model,
    get_core_api_providers_for_frontend,
)
from utils.config_manager import get_config_manager, get_reserved, set_reserved
from utils.doubao_tts import (
    DOUBAO_TTS_DEFAULT_RESOURCE_ID,
    DOUBAO_VOICE_STORAGE_KEY,
)
from utils.tts import provider_registry as tts_provider_registry
from utils.tts.providers.elevenlabs import ELEVENLABS_TTS_DEFAULT_MODEL
from utils.tts.providers.mimo import MIMO_TTS_VOICECLONE_MODEL
from utils.voice_config import VoiceConfig, read_legacy_voice_id, to_legacy_voice_id

router = APIRouter(prefix="/api/v1/admin")

# core_config.json 中的敏感凭证字段：返回给前端前必须脱敏，PUT 时只有非空值才会覆盖。
CORE_CONFIG_KEY_FIELDS = (
    "coreApiKey",
    "assistApiKeyQwen",
    "assistApiKeyQwenIntl",
    "assistApiKeyOpenai",
    "assistApiKeyGlm",
    "assistApiKeyStep",
    "assistApiKeySilicon",
    "assistApiKeyGemini",
    "assistApiKeyKimi",
    "assistApiKeyKimiCode",
    "assistApiKeyDeepseek",
    "assistApiKeyDoubao",
    "assistApiKeyDoubaoTts",
    "assistApiKeyMinimax",
    "assistApiKeyMinimaxIntl",
    "assistApiKeyMimo",
    "assistApiKeyMimoTokenPlan",
    "assistApiKeyElevenlabs",
    "assistApiKeyClaude",
    "assistApiKeyGrok",
    "ttsModelApiKey",
)

# Only providers whose current runtime declares the ``clone`` capability belong
# in this admin catalog.  Models are deliberately an allowlist matching the
# worker implementations; accepting an arbitrary model name would create a
# configuration the UI claims to support but the worker silently ignores.
VOICE_CLONE_PROVIDER_SPECS = {
    "gptsovits": {
        "name": "GPT-SoVITS（本地）",
        "registry_key": "gptsovits",
        "models": ("GPT-SoVITS-v3",),
        "registration_mode": "local_id",
        "credential_hint": "使用 TTS 模型 URL 指向本地 GPT-SoVITS v3 服务",
    },
    "vllm_omni": {
        "name": "vLLM-Omni（本地）",
        "registry_key": "vllm_omni",
        "models": ("Qwen3-TTS",),
        "registration_mode": "saved_sample",
        "credential_hint": "只能选择已经随参考音频导入的克隆音色",
    },
    "cosyvoice": {
        "name": "阿里百炼 CosyVoice",
        "registry_key": "cosyvoice",
        "models": (get_cosyvoice_clone_model("cosyvoice"),),
        "registration_mode": "remote_id",
        "credential_hint": "需要 Qwen/CosyVoice 国内版 API Key",
    },
    "cosyvoice_intl": {
        "name": "阿里国际版 CosyVoice",
        "registry_key": "cosyvoice",
        "models": (get_cosyvoice_clone_model("cosyvoice_intl"),),
        "registration_mode": "remote_id",
        "credential_hint": "需要 Qwen 国际版 API Key",
    },
    "minimax": {
        "name": "MiniMax 国内版",
        "registry_key": "minimax",
        "models": ("speech-2.8-turbo",),
        "registration_mode": "remote_id",
        "credential_hint": "需要 MiniMax 国内版 API Key",
    },
    "minimax_intl": {
        "name": "MiniMax 国际版",
        "registry_key": "minimax",
        "models": ("speech-2.8-turbo",),
        "registration_mode": "remote_id",
        "credential_hint": "需要 MiniMax 国际版 API Key",
    },
    "elevenlabs": {
        "name": "ElevenLabs",
        "registry_key": "elevenlabs",
        "models": (ELEVENLABS_TTS_DEFAULT_MODEL,),
        "registration_mode": "remote_id",
        "credential_hint": "需要 ElevenLabs API Key",
    },
    "mimo": {
        "name": "小米 MiMo",
        "registry_key": "mimo",
        "models": (MIMO_TTS_VOICECLONE_MODEL,),
        "registration_mode": "saved_sample",
        "credential_hint": "只能选择已经随参考音频导入的克隆音色",
    },
    "doubao_tts": {
        "name": "豆包声音复刻",
        "registry_key": "doubao_tts",
        "models": (DOUBAO_TTS_DEFAULT_RESOURCE_ID,),
        "registration_mode": "remote_id",
        "credential_hint": "需要豆包 TTS API Key",
    },
}


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


class CoreConfigUpdate(BaseModel):
    """Partial update of core_config.json. None/empty means "keep the current value"."""

    coreApi: str | None = Field(default=None, min_length=1, max_length=50)
    assistApi: str | None = Field(default=None, min_length=1, max_length=50)
    useMimoTokenPlan: bool | None = None
    ttsModelUrl: str | None = Field(default=None, max_length=1024)
    coreApiKey: str | None = Field(default=None, max_length=512)
    assistApiKeyQwen: str | None = Field(default=None, max_length=512)
    assistApiKeyQwenIntl: str | None = Field(default=None, max_length=512)
    assistApiKeyOpenai: str | None = Field(default=None, max_length=512)
    assistApiKeyGlm: str | None = Field(default=None, max_length=512)
    assistApiKeyStep: str | None = Field(default=None, max_length=512)
    assistApiKeySilicon: str | None = Field(default=None, max_length=512)
    assistApiKeyGemini: str | None = Field(default=None, max_length=512)
    assistApiKeyKimi: str | None = Field(default=None, max_length=512)
    assistApiKeyKimiCode: str | None = Field(default=None, max_length=512)
    assistApiKeyDeepseek: str | None = Field(default=None, max_length=512)
    assistApiKeyDoubao: str | None = Field(default=None, max_length=512)
    assistApiKeyDoubaoTts: str | None = Field(default=None, max_length=512)
    assistApiKeyMinimax: str | None = Field(default=None, max_length=512)
    assistApiKeyMinimaxIntl: str | None = Field(default=None, max_length=512)
    assistApiKeyMimo: str | None = Field(default=None, max_length=512)
    assistApiKeyMimoTokenPlan: str | None = Field(default=None, max_length=512)
    assistApiKeyElevenlabs: str | None = Field(default=None, max_length=512)
    assistApiKeyClaude: str | None = Field(default=None, max_length=512)
    assistApiKeyGrok: str | None = Field(default=None, max_length=512)
    ttsModelApiKey: str | None = Field(default=None, max_length=512)


class VoiceCloneBindingUpdate(BaseModel):
    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=128)
    voice_id: str = Field(min_length=1, max_length=256)
    display_name: str | None = Field(default=None, max_length=100)
    rights_confirmed: bool = False


def _mask_key(value: object) -> str:
    """Mask a credential for display: first 5 + last 5 chars, middle hidden.

    Short values are fully hidden so their length is never leaked.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 10:
        return f"{text[:5]}…{text[-5:]}"
    return "…"


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


def _clone_provider_spec(provider: str) -> dict:
    """Return a clone-capable provider spec backed by the runtime registry."""
    # Importing the package registers every worker adapter.  This is intentionally
    # lazy so importing the router alone remains light for static tooling.
    import main_logic.tts_client  # noqa: F401

    key = str(provider or "").strip().lower()
    spec = VOICE_CLONE_PROVIDER_SPECS.get(key)
    registered = tts_provider_registry.get(
        str((spec or {}).get("registry_key") or "")
    )
    if spec is None or registered is None or "clone" not in registered.capabilities:
        raise HTTPException(status_code=400, detail="unsupported voice-clone provider")
    return spec


def _clone_provider_storage_key(manager, provider: str) -> str:
    if provider in {"cosyvoice", "cosyvoice_intl"}:
        return str(
            manager.get_cosyvoice_clone_runtime(provider).get("storage_key") or ""
        ).strip()
    if provider in {"minimax", "minimax_intl"}:
        api_key = str(manager.get_tts_api_key(provider) or "").strip()
        marker = "__MINIMAX_INTL__" if provider == "minimax_intl" else "__MINIMAX__"
        return f"{marker}{api_key[-8:]}" if api_key else ""
    if provider == "elevenlabs":
        api_key = str(manager.get_tts_api_key(provider) or "").strip()
        return f"__ELEVENLABS__{api_key[-8:]}" if api_key else ""
    if provider == "mimo":
        api_key = str(manager.get_tts_api_key(provider) or "").strip()
        return f"__MIMO__{api_key[-8:]}" if api_key else ""
    if provider == "doubao_tts":
        api_key = str(manager.get_tts_api_key(provider) or "").strip()
        return f"{DOUBAO_VOICE_STORAGE_KEY}{api_key[-8:]}" if api_key else ""
    if provider == "vllm_omni":
        return "__VLLM_OMNI__"
    return ""


def _clone_provider_ready(manager, provider: str) -> bool:
    if provider in {"gptsovits", "vllm_omni"}:
        return True
    if provider in {"cosyvoice", "cosyvoice_intl"}:
        return bool(
            str(
                manager.get_cosyvoice_clone_runtime(provider).get("api_key") or ""
            ).strip()
        )
    return bool(str(manager.get_tts_api_key(provider) or "").strip())


def _voice_ref_for_provider(provider: str, voice_id: str) -> str:
    ref = str(voice_id or "").strip()
    prefix = (
        "eleven:"
        if provider == "elevenlabs"
        else "gsv:"
        if provider == "gptsovits"
        else ""
    )
    if prefix and ref.startswith(prefix):
        ref = ref[len(prefix):].strip()
    if not ref or any(character in ref for character in "\r\n\0"):
        raise HTTPException(status_code=422, detail="invalid voice id")
    return ref


def _voice_library(manager) -> tuple[dict, list[dict]]:
    """Return runtime metadata plus a compact admin-safe clone voice list."""
    try:
        voices = manager.get_voices_for_current_api(for_listing=False) or {}
    except Exception:
        voices = {}
    listing: list[dict] = []
    for voice_id, metadata in voices.items():
        if not isinstance(metadata, dict):
            continue
        provider = str(metadata.get("provider") or "").strip().lower()
        if provider not in VOICE_CLONE_PROVIDER_SPECS:
            continue
        source = str(metadata.get("source") or "clone").strip().lower()
        if source != "clone":
            continue
        ref = str(voice_id or "").strip()
        prefix = (
            "eleven:"
            if provider == "elevenlabs"
            else "gsv:"
            if provider == "gptsovits"
            else ""
        )
        if prefix and ref.startswith(prefix):
            ref = ref[len(prefix):].strip()
        if not ref or any(character in ref for character in "\r\n\0"):
            continue
        listing.append(
            {
                "voiceId": ref,
                "name": str(metadata.get("name") or ref),
                "provider": provider,
                "model": str(
                    metadata.get("clone_model")
                    or metadata.get("doubao_resource_id")
                    or VOICE_CLONE_PROVIDER_SPECS[provider]["models"][0]
                ),
            }
        )
    listing.sort(key=lambda item: (item["provider"], item["name"].casefold()))
    return voices, listing


async def _voice_config_payload(manager) -> dict:
    characters = await manager.aload_characters()
    current_character = characters.get("当前猫娘") or next(
        iter(characters.get("猫娘", {})), ""
    )
    character = characters.get("猫娘", {}).get(current_character, {})
    raw_voice = get_reserved(
        character, "voice_id", default="", legacy_keys=("voice_id",)
    )
    voice_config = VoiceConfig.from_any(raw_voice)
    if voice_config.ref and not voice_config.provider:
        voice_config = manager.normalize_voice_id_to_config(
            read_legacy_voice_id(raw_voice)
        )
    runtime_voice_id = read_legacy_voice_id(raw_voice)
    voices, saved_voices = _voice_library(manager)
    metadata = voices.get(runtime_voice_id)
    if not isinstance(metadata, dict):
        metadata = {}
    provider = voice_config.provider or str(metadata.get("provider") or "")
    provider_spec = VOICE_CLONE_PROVIDER_SPECS.get(provider, {})
    model = str(
        metadata.get("clone_model")
        or metadata.get("doubao_resource_id")
        or (provider_spec.get("models") or ("",))[0]
    )
    providers = []
    for key, spec in VOICE_CLONE_PROVIDER_SPECS.items():
        try:
            _clone_provider_spec(key)
        except HTTPException:
            continue
        providers.append(
            {
                "key": key,
                "name": spec["name"],
                "kind": tts_provider_registry.get(spec["registry_key"]).kind,
                "models": list(spec["models"]),
                "registrationMode": spec["registration_mode"],
                "credentialHint": spec["credential_hint"],
                "ready": _clone_provider_ready(manager, key),
            }
        )
    return {
        "character": current_character,
        "current": {
            "configured": bool(voice_config.ref),
            "cloneConfigured": bool(
                voice_config.ref
                and voice_config.source == "clone"
                and provider in VOICE_CLONE_PROVIDER_SPECS
            ),
            "source": voice_config.source,
            "provider": provider,
            "model": model,
            "voiceId": voice_config.ref,
        },
        "providers": providers,
        "savedVoices": saved_voices,
    }


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


@router.get("/voice-config")
async def get_voice_config(request: Request) -> dict:
    """Return clone-capable providers/models and the active character binding."""
    _auth(request)
    return await _voice_config_payload(get_config_manager())


@router.put("/voice-config")
async def update_voice_config(
    payload: VoiceCloneBindingUpdate, request: Request
) -> dict:
    """Bind an existing, licensed cloned voice to the current public character.

    Remote-ID providers may register an upstream clone ID here.  Providers whose
    clone identity is the reference sample itself (MiMo/vLLM-Omni) can only bind
    a sample already present in the private voice library; this endpoint never
    accepts or exposes audio blobs.
    """
    _auth(request, write=True)
    if not payload.rights_confirmed:
        raise HTTPException(
            status_code=400,
            detail="voice rights confirmation is required",
        )

    manager = get_config_manager()
    provider = payload.provider.strip().lower()
    spec = _clone_provider_spec(provider)
    model = payload.model.strip()
    if model not in spec["models"]:
        raise HTTPException(
            status_code=400,
            detail="model is not supported by the selected provider",
        )
    if not _clone_provider_ready(manager, provider):
        raise HTTPException(
            status_code=409,
            detail="selected voice provider is missing its private credential",
        )

    characters = await manager.aload_characters()
    current = characters.get("当前猫娘") or next(
        iter(characters.get("猫娘", {})), ""
    )
    if not current or current not in characters.get("猫娘", {}):
        raise HTTPException(status_code=409, detail="current character is missing")

    voice_ref = _voice_ref_for_provider(provider, payload.voice_id)
    if provider in {"cosyvoice", "cosyvoice_intl"} and manager.is_legacy_cosyvoice_id(
        voice_ref
    ):
        raise HTTPException(
            status_code=400,
            detail="legacy CosyVoice v2/v3 voice ids are no longer supported",
        )
    voice_config = VoiceConfig(source="clone", provider=provider, ref=voice_ref)
    runtime_voice_id = to_legacy_voice_id(voice_config)
    voice_storage = manager.load_voice_storage() or {}
    storage_key = _clone_provider_storage_key(manager, provider)

    if spec["registration_mode"] == "saved_sample":
        existing = (voice_storage.get(storage_key) or {}).get(runtime_voice_id)
        if not isinstance(existing, dict) or not str(
            existing.get("clone_sample_b64") or ""
        ).strip():
            raise HTTPException(
                status_code=409,
                detail="this provider requires a previously imported reference-audio voice",
            )
        if str(existing.get("provider") or "").strip() != provider:
            raise HTTPException(status_code=409, detail="saved voice provider mismatch")
    elif spec["registration_mode"] == "remote_id":
        if not storage_key:
            raise HTTPException(
                status_code=409,
                detail="selected voice provider has no writable private storage",
            )
        for bucket_key, bucket in voice_storage.items():
            if bucket_key == storage_key or not isinstance(bucket, dict):
                continue
            collision = bucket.get(runtime_voice_id)
            collision_provider = (
                str(collision.get("provider") or "").strip()
                if isinstance(collision, dict)
                else ""
            )
            if collision is not None and collision_provider != provider:
                raise HTTPException(
                    status_code=409,
                    detail="voice id is already owned by another provider",
                )
        bucket = voice_storage.setdefault(storage_key, {})
        previous = bucket.get(runtime_voice_id)
        metadata = dict(previous) if isinstance(previous, dict) else {}
        metadata.update(
            {
                "name": str(payload.display_name or "").strip() or voice_ref,
                "source": "clone",
                "provider": provider,
                "clone_model": model,
            }
        )
        if provider == "doubao_tts":
            metadata["doubao_resource_id"] = model
        bucket[runtime_voice_id] = metadata
        await asyncio.to_thread(manager.save_voice_storage, voice_storage)

    set_reserved(
        characters["猫娘"][current], "voice_id", voice_config.to_dict()
    )
    await manager.asave_characters(characters)

    if provider == "gptsovits":
        raw_core = manager.load_json_config("core_config.json", {}) or {}
        raw_core.update(
            {
                "ttsModelProvider": "gptsovits",
                "ttsModelId": model,
                "ttsVoiceId": runtime_voice_id,
            }
        )
        await asyncio.to_thread(
            manager.save_json_config, "core_config.json", raw_core
        )

    service = request.app.state.room_service
    await service.speech.reconfigure()
    await service.store.audit(
        "voice.clone.bind",
        "character",
        current,
        {"provider": provider, "model": model},
    )
    return {"ok": True, **(await _voice_config_payload(manager))}


@router.delete("/voice-config")
async def clear_voice_config(request: Request) -> dict:
    """Disable the current character voice without deleting its private library."""
    _auth(request, write=True)
    manager = get_config_manager()
    characters = await manager.aload_characters()
    current = characters.get("当前猫娘") or next(
        iter(characters.get("猫娘", {})), ""
    )
    if not current or current not in characters.get("猫娘", {}):
        raise HTTPException(status_code=409, detail="current character is missing")
    set_reserved(characters["猫娘"][current], "voice_id", "")
    await manager.asave_characters(characters)
    await request.app.state.room_service.speech.reconfigure()
    await request.app.state.room_service.store.audit(
        "voice.binding.clear", "character", current
    )
    return {"ok": True, **(await _voice_config_payload(manager))}


@router.get("/core-config")
async def get_core_config(request: Request) -> dict:
    """Return core_config.json for the admin panel.

    All credential fields are masked server-side: the frontend only ever sees
    the first 5 + last 5 characters, never the full key.
    """
    _auth(request)
    raw = get_config_manager().load_json_config("core_config.json", {}) or {}
    resolved_urls = {
        str(key): str(value)
        for key, value in dict(raw.get("resolvedProviderUrls", {}) or {}).items()
        if "agent" not in str(key).casefold() and "mcp" not in str(key).casefold()
    }
    response = {
        "coreApi": str(raw.get("coreApi", "") or ""),
        "assistApi": str(raw.get("assistApi", "") or ""),
        "useMimoTokenPlan": bool(raw.get("useMimoTokenPlan", False)),
        "ttsModelUrl": str(raw.get("ttsModelUrl", "") or ""),
        "resolvedProviderUrls": resolved_urls,
        "coreProviders": get_core_api_providers_for_frontend(),
        "assistProviders": get_assist_api_providers_for_frontend(),
    }
    for field in CORE_CONFIG_KEY_FIELDS:
        response[field] = _mask_key(raw.get(field))
    return response


@router.put("/core-config")
async def update_core_config(payload: CoreConfigUpdate, request: Request) -> dict:
    """Apply a partial update to core_config.json.

    Only fields present in the body are touched; unknown fields in the file
    are preserved. Empty credential strings are ignored so a key can never be
    accidentally cleared by a blank input.
    """
    _auth(request, write=True)
    manager = get_config_manager()
    raw = manager.load_json_config("core_config.json", {}) or {}
    submitted = payload.model_dump(exclude_unset=True)
    allowed_provider_keys = {
        "coreApi": {
            str(item.get("key") or "")
            for item in get_core_api_providers_for_frontend()
        },
        "assistApi": {
            str(item.get("key") or "")
            for item in get_assist_api_providers_for_frontend()
        },
    }
    for field, allowed in allowed_provider_keys.items():
        value = str(submitted.get(field) or "").strip()
        if value and value not in allowed:
            raise HTTPException(status_code=400, detail=f"unsupported {field}")
    changed: list[str] = []
    for field, value in submitted.items():
        if isinstance(value, bool):
            if raw.get(field) != value:
                raw[field] = value
                changed.append(field)
        elif isinstance(value, str) and value.strip():
            value = value.strip()
            if raw.get(field) != value:
                raw[field] = value
                changed.append(field)
    if changed:
        await asyncio.to_thread(manager.save_json_config, "core_config.json", raw)
        await request.app.state.room_service.speech.reconfigure()
        await request.app.state.room_service.store.audit(
            "core_config.update", "config", "core", {"fields": changed}
        )
    return {"ok": True, "changed": changed}

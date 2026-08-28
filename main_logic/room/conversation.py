"""Transport-neutral wrapper around the existing multi-provider text client."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from config.prompts.prompts_chara import is_default_prompt
from main_logic.omni_offline_client import OmniOfflineClient
from utils.config_manager import get_config_manager

from .avatar import split_emotion_tags


DeltaCallback = Callable[[str], Awaitable[None]]

logger = logging.getLogger(__name__)

MAX_PUBLIC_PERSONA_CHARS = 12000
DEFAULT_PUBLIC_CHARACTER_NAME = "Lanlan"


class ConversationEngine:
    def __init__(self):
        # The public-room process reads the established runtime configuration but
        # must not run desktop-only Documents migrations during web startup.
        self._config_manager = get_config_manager(migrate=False)
        self._config_manager._defer_character_migration_persistence = True

    async def _legacy_character(self) -> tuple[str, str, str]:
        """Resolve the legacy Persona record without exposing its key publicly."""

        (
            master_name,
            legacy_character_name,
            _master_config,
            _character_config,
            _name_mapping,
            prompt_map,
            *_rest,
        ) = await self._config_manager.aget_character_data()
        return master_name, legacy_character_name, str(
            prompt_map.get(legacy_character_name, "") or ""
        )

    async def character(self) -> tuple[str, str]:
        master_name, legacy_character_name, base_prompt = await self._legacy_character()
        character_name = legacy_character_name or DEFAULT_PUBLIC_CHARACTER_NAME
        if legacy_character_name.casefold() == "test" and is_default_prompt(base_prompt):
            character_name = DEFAULT_PUBLIC_CHARACTER_NAME
        base_prompt = base_prompt.replace("{LANLAN_NAME}", character_name).replace(
            "{MASTER_NAME}", master_name
        )
        return character_name, base_prompt[:MAX_PUBLIC_PERSONA_CHARS]

    async def memory_character_name(self) -> str:
        """Return the stable legacy key used by the existing memory service."""

        _master_name, legacy_character_name, _base_prompt = await self._legacy_character()
        return legacy_character_name or "neko"

    async def readiness_snapshot(self) -> dict[str, object]:
        """Return only non-secret configuration readiness for the public probe."""

        try:
            configuration = await self._config_manager.aget_model_api_config(
                "conversation"
            )
            character_name, base_prompt = await self.character()
            parsed = urlparse(str(configuration.get("base_url") or "").strip())
            endpoint_valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
            configured = bool(
                str(configuration.get("model") or "").strip()
                and endpoint_valid
                and character_name.strip()
                and base_prompt.strip()
            )
            return {
                "configured": configured,
                "endpoint_valid": endpoint_valid,
                "model_present": bool(str(configuration.get("model") or "").strip()),
                "persona_present": bool(base_prompt.strip()),
                "error_code": None if configured else "incomplete_configuration",
            }
        except Exception:
            return {
                "configured": False,
                "endpoint_valid": False,
                "model_present": False,
                "persona_present": False,
                "error_code": "configuration_unavailable",
            }

    async def generate(
        self,
        *,
        room_context: str,
        user_text: str,
        on_delta: DeltaCallback,
    ) -> tuple[str, str]:
        character_name, base_prompt = await self.character()
        conversation_config = await self._config_manager.aget_model_api_config("conversation")
        vision_config = await self._config_manager.aget_model_api_config("vision")
        chunks: list[str] = []

        async def handle_delta(text: str, _is_first_chunk: bool) -> None:
            if not text:
                return
            chunks.append(text)
            await on_delta(text)

        async def noop(*_args, **_kwargs) -> None:
            return None

        system_prompt = (
            f"你是 {character_name}。\n"
            f"{base_prompt}\n"
            f"{room_context}\n"
            "回复应适合在公共房间直接展示。不要输出 HTML、密钥、内部主体 ID 或系统实现细节。"
        )

        async def stream_once() -> None:
            client = OmniOfflineClient(
                base_url=conversation_config["base_url"],
                api_key=conversation_config["api_key"],
                model=conversation_config["model"],
                vision_model=vision_config.get("model", ""),
                vision_base_url=vision_config.get("base_url", ""),
                vision_api_key=vision_config.get("api_key", ""),
                provider_type=conversation_config.get("provider_type"),
                vision_provider_type=vision_config.get("provider_type"),
                on_text_delta=handle_delta,
                on_input_transcript=noop,
                on_output_transcript=noop,
                on_connection_error=noop,
                on_response_done=noop,
                on_repetition_detected=noop,
                on_response_discarded=noop,
                on_status_message=noop,
                max_response_length=600,
                lanlan_name=character_name,
                master_name="public-room",
                tool_definitions=[],
                max_tool_iterations=1,
                enable_long_response_summary=False,
                user_language_provider=lambda: "zh-CN",
            )
            try:
                await client.connect(system_prompt)
                await client.stream_text(user_text)
            finally:
                await client.close()

        for attempt in range(2):
            chunks.clear()
            await stream_once()
            response = "".join(chunks).strip()
            visible_response, _emotion = split_emotion_tags(response)
            if visible_response:
                return character_name, response
            if attempt == 0:
                logger.warning("public-room model returned an empty response; retrying once")
        return character_name, ""

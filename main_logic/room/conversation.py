"""Transport-neutral wrapper around the existing multi-provider text client."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from main_logic.omni_offline_client import OmniOfflineClient
from utils.config_manager import get_config_manager


DeltaCallback = Callable[[str], Awaitable[None]]


class ConversationEngine:
    def __init__(self):
        self._config_manager = get_config_manager()

    async def character(self) -> tuple[str, str]:
        (
            master_name,
            character_name,
            _master_config,
            _character_config,
            _name_mapping,
            prompt_map,
            *_rest,
        ) = await self._config_manager.aget_character_data()
        base_prompt = str(prompt_map.get(character_name, "") or "")
        base_prompt = base_prompt.replace("{LANLAN_NAME}", character_name).replace(
            "{MASTER_NAME}", master_name
        )
        return character_name or "NEKO", base_prompt

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
        system_prompt = (
            f"你是 {character_name}。\n"
            f"{base_prompt}\n"
            f"{room_context}\n"
            "回复应适合在公共房间直接展示。不要输出 HTML、密钥、内部主体 ID 或系统实现细节。"
        )
        try:
            await client.connect(system_prompt)
            await client.stream_text(user_text)
        finally:
            await client.close()
        return character_name, "".join(chunks).strip()

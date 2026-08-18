"""Generate one shared PCM/WAV asset for each public-room reply."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import wave
from datetime import datetime
from queue import Queue
from threading import Thread
from pathlib import Path
from typing import Any
from uuid import uuid4

from main_logic.tts_client import (
    TTS_SHUTDOWN_SENTINEL,
    dummy_tts_worker,
    get_tts_worker,
)
from utils.config_manager import _as_bool, get_config_manager, get_reserved
from utils.gptsovits_config import is_gsv_disabled_voice_id
from utils.tts.native_voice_registry import (
    is_free_preset_voice_id,
    resolve_native_voice_for_routing,
)
from utils.voice_config import read_legacy_voice_id

logger = logging.getLogger(__name__)

_TTS_BRACKETED_CONTENT_RE = re.compile(
    r"\([^()]*\)|（[^（）]*）|\[[^\[\]]*\]|【[^【】]*】|\{[^{}]*\}|<[^<>]*>"
)
_TTS_HORIZONTAL_WHITESPACE_RE = re.compile(r"[^\S\r\n]+")
_TTS_SPACE_BEFORE_PUNCTUATION_RE = re.compile(r" +([,，。！？!?；;、])")


class SpeechUnavailable(RuntimeError):
    pass


class SpeechService:
    """Own a single legacy TTS worker and publish completed WAV files.

    The desktop runtime streams audio independently to one websocket. Public
    rooms instead collect that same 48 kHz PCM stream once, write one immutable
    WAV file, and broadcast its URL to every visitor.
    """

    def __init__(self, audio_root: Path):
        self.audio_root = audio_root.resolve()
        self.audio_root.mkdir(parents=True, exist_ok=True)
        # Public-room startup must not trigger legacy desktop storage migration.
        self._config_manager = get_config_manager(migrate=False)
        self._request_queue: Queue | None = None
        self._response_queue: Queue | None = None
        self._thread: Thread | None = None
        self._provider_key: str | None = None
        self._ready = False
        self._start_lock = asyncio.Lock()
        self._synthesis_lock = asyncio.Lock()
        self._disabled = bool(
            (self._config_manager.get_core_config() or {}).get("DISABLE_TTS", False)
        )

    @property
    def configured(self) -> bool:
        """Report whether speech is enabled without resolving a legacy voice at boot.

        Route resolution loads the editable character registry and may perform a
        compatibility write.  That belongs to the first synthesis request, not
        application construction, otherwise a stale desktop file can prevent the
        entire public web service from binding its port.
        """
        return not self._disabled

    def _resolve_route(self) -> tuple[Any, str, str | None, str]:
        core_config = self._config_manager.get_core_config() or {}
        realtime = self._config_manager.get_model_api_config("realtime")
        core_api_type = str(
            realtime.get("api_type") or core_config.get("CORE_API_TYPE") or ""
        ).strip()
        character_data = self._config_manager.get_character_data()
        character_name = character_data[1]
        characters = character_data[3]
        raw_voice = get_reserved(
            characters.get(character_name, {}),
            "voice_id",
            default="",
            legacy_keys=("voice_id",),
        )
        voice_id = read_legacy_voice_id(raw_voice)
        free_preset = is_free_preset_voice_id(voice_id)
        if free_preset and core_api_type != "free":
            voice_id = ""
            free_preset = False

        _, native_voice = resolve_native_voice_for_routing(
            core_api_type,
            voice_id,
            self._config_manager.voice_id_exists_in_any_storage,
            realtime_base_url=str(realtime.get("base_url") or ""),
        )
        gsv_voice_id = str(core_config.get("TTS_VOICE_ID") or "")
        gsv_enabled = _as_bool(core_config.get("GPTSOVITS_ENABLED"), False) and not (
            is_gsv_disabled_voice_id(gsv_voice_id)
        )
        has_custom = not native_voice and (
            gsv_enabled or (bool(voice_id) and not free_preset)
        )
        worker, api_key_override, provider_key = get_tts_worker(
            core_api_type=core_api_type,
            has_custom_voice=has_custom,
            voice_id=voice_id,
        )
        tts_config = self._config_manager.get_model_api_config(
            "tts_custom" if has_custom else "tts_default"
        )
        api_key = (
            api_key_override
            if api_key_override is not None
            else str(tts_config.get("api_key") or "")
        )
        return worker, api_key, provider_key, voice_id

    async def _ensure_started(self) -> None:
        if self._disabled:
            raise SpeechUnavailable("TTS is disabled")
        if self._ready and self._thread is not None and self._thread.is_alive():
            return
        async with self._start_lock:
            if self._ready and self._thread is not None and self._thread.is_alive():
                return
            if self._thread is not None:
                await self._stop_worker()
            worker, api_key, provider_key, voice_id = await asyncio.to_thread(
                self._resolve_route
            )
            if worker is dummy_tts_worker or provider_key is None:
                self._disabled = True
                raise SpeechUnavailable("no configured TTS provider")
            self._request_queue = Queue()
            self._response_queue = Queue()
            self._provider_key = provider_key
            self._thread = Thread(
                target=worker,
                args=(self._request_queue, self._response_queue, api_key, voice_id),
                daemon=True,
                name="public-room-tts",
            )
            self._thread.start()
            try:
                ready = await asyncio.wait_for(
                    asyncio.to_thread(self._response_queue.get), timeout=15.0
                )
            except TimeoutError as exc:
                await self._stop_worker()
                raise SpeechUnavailable("TTS worker readiness timed out") from exc
            if ready != ("__ready__", True):
                await self._stop_worker()
                raise SpeechUnavailable("TTS worker failed to initialize")
            self._ready = True

    async def _stop_worker(self) -> None:
        request_queue = self._request_queue
        thread = self._thread
        self._ready = False
        if request_queue is not None and thread is not None and thread.is_alive():
            request_queue.put((TTS_SHUTDOWN_SENTINEL, None))
            await asyncio.to_thread(thread.join, 3.0)
        self._thread = None
        self._request_queue = None
        self._response_queue = None
        self._provider_key = None

    async def synthesize(self, text: str) -> dict[str, Any]:
        normalized = self.prepare_text(text)
        if not normalized:
            raise ValueError("speech text is empty")
        async with self._synthesis_lock:
            await self._ensure_started()
            assert self._request_queue is not None
            assert self._response_queue is not None
            speech_id = f"speech_{uuid4().hex}"
            self._request_queue.put((speech_id, normalized))
            self._request_queue.put((None, None))
            chunks: list[bytes] = []
            deadline = time.monotonic() + float(
                os.environ.get("NEKO_PUBLIC_TTS_TIMEOUT_SECONDS", "90")
            )
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._request_queue.put(("__interrupt__", None))
                    await self._stop_worker()
                    raise SpeechUnavailable("TTS synthesis timed out")
                try:
                    item = await asyncio.wait_for(
                        asyncio.to_thread(self._response_queue.get), timeout=remaining
                    )
                except TimeoutError as exc:
                    self._request_queue.put(("__interrupt__", None))
                    await self._stop_worker()
                    raise SpeechUnavailable("TTS synthesis timed out") from exc
                if isinstance(item, tuple):
                    if len(item) == 2 and item[0] == "__audio_done__":
                        if str(item[1] or "") == speech_id:
                            break
                        continue
                    if len(item) == 2 and item[0] == "__error__":
                        raise SpeechUnavailable("TTS provider returned an error")
                    if len(item) == 3 and item[0] == "__audio__":
                        if str(item[1] or "") == speech_id:
                            chunks.append(self._as_pcm_bytes(item[2]))
                        continue
                    continue
                chunks.append(self._as_pcm_bytes(item))
            pcm = b"".join(chunk for chunk in chunks if chunk)
            if not pcm:
                raise SpeechUnavailable("TTS provider returned empty audio")
            filename = f"{speech_id}.wav"
            destination = self.audio_root / filename
            await asyncio.to_thread(self._write_wav, destination, pcm)
            return {
                "speech_id": speech_id,
                "url": f"/speech-assets/{filename}",
                "content_type": "audio/wav",
                "sample_rate": 48000,
                "provider": self._provider_key,
            }

    async def synthesize_complete(self, text: str) -> dict[str, Any]:
        """Synthesize punctuation-bounded requests, then publish one continuous WAV.

        Some compatible realtime TTS upstreams terminate an utterance at an
        internal punctuation boundary when given a long delta. Keeping those
        requests short avoids losing the suffix, while the public room still
        exposes one audio asset and therefore one uninterrupted playback.
        """
        prepared = self.prepare_text(text)
        segments = self._split_tts_segments(prepared)
        if not segments:
            raise ValueError("speech text is empty after removing bracketed content")
        if len(segments) <= 1:
            return await self.synthesize(prepared)

        temporary_paths: list[Path] = []
        pcm_parts: list[bytes] = []
        provider: str | None = None
        try:
            for segment in segments:
                payload = await self.synthesize(segment)
                filename = Path(str(payload["url"])).name
                path = self.audio_root / filename
                temporary_paths.append(path)
                pcm_parts.append(await asyncio.to_thread(self._read_wav_pcm, path))
                provider = str(payload.get("provider") or provider or "") or None

            speech_id = f"speech_{uuid4().hex}"
            destination = self.audio_root / f"{speech_id}.wav"
            await asyncio.to_thread(self._write_wav, destination, b"".join(pcm_parts))
            return {
                "speech_id": speech_id,
                "url": f"/speech-assets/{destination.name}",
                "content_type": "audio/wav",
                "sample_rate": 48000,
                "provider": provider,
            }
        finally:
            for path in temporary_paths:
                await asyncio.to_thread(path.unlink, missing_ok=True)

    @staticmethod
    def prepare_text(text: str) -> str:
        """Remove non-spoken bracketed directions before an upstream TTS call.

        Repeating the regular-expression substitution also removes nested or
        mixed bracket pairs from the inside out. The original assistant text
        remains untouched in room history and on the public timeline.
        """
        normalized = str(text or "")
        while True:
            cleaned = _TTS_BRACKETED_CONTENT_RE.sub(" ", normalized)
            if cleaned == normalized:
                break
            normalized = cleaned
        normalized = _TTS_HORIZONTAL_WHITESPACE_RE.sub(" ", normalized)
        normalized = re.sub(r" *\n+ *", "\n", normalized)
        normalized = _TTS_SPACE_BEFORE_PUNCTUATION_RE.sub(r"\1", normalized)
        return normalized.strip()

    @staticmethod
    def _split_tts_segments(text: str, *, max_chars: int = 96) -> list[str]:
        normalized = str(text or "").strip()
        if not normalized:
            return []
        segments: list[str] = []
        buffer: list[str] = []
        endings = frozenset("，、,；;。！？!?")
        for character in normalized:
            buffer.append(character)
            if character in endings or len(buffer) >= max_chars:
                segment = "".join(buffer).strip()
                if segment:
                    segments.append(segment)
                buffer.clear()
        tail = "".join(buffer).strip()
        if tail:
            segments.append(tail)
        return segments

    @staticmethod
    def _as_pcm_bytes(value: Any) -> bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if hasattr(value, "tobytes"):
            return value.tobytes()
        raise SpeechUnavailable("TTS provider returned an unsupported audio chunk")

    @staticmethod
    def _write_wav(destination: Path, pcm: bytes) -> None:
        temporary = destination.with_suffix(".tmp")
        with wave.open(str(temporary), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(pcm)
        temporary.replace(destination)

    @staticmethod
    def _read_wav_pcm(path: Path) -> bytes:
        with wave.open(str(path), "rb") as source:
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getframerate() != 48000
            ):
                raise SpeechUnavailable("TTS segment has an unsupported WAV format")
            return source.readframes(source.getnframes())

    async def cleanup_before(self, cutoff: datetime) -> int:
        return await asyncio.to_thread(self._cleanup_before_sync, cutoff.timestamp())

    def _cleanup_before_sync(self, cutoff_timestamp: float) -> int:
        removed = 0
        for pattern in ("*.wav", "*.tmp"):
            for candidate in self.audio_root.glob(pattern):
                try:
                    if candidate.is_file() and candidate.stat().st_mtime < cutoff_timestamp:
                        candidate.unlink()
                        removed += 1
                except FileNotFoundError:
                    continue
        return removed

    async def shutdown(self) -> None:
        await self._stop_worker()

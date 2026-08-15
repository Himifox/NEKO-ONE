"""Preflight or explicitly run the real LLM, Memory, and TTS acceptance path."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from config import MEMORY_SERVER_PORT
from main_logic.room.conversation import ConversationEngine
from main_logic.room.memory_facade import MemoryFacade
from main_logic.room.models import RoomMessage, Visitor, utc_now
from main_logic.room.speech import SpeechService
from main_logic.tts_client import dummy_tts_worker
from utils.config_manager import get_config_manager


ACKNOWLEDGEMENT = "I_ACCEPT_PROVIDER_COSTS_AND_TEST_DATA"


class ProviderAcceptanceError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_endpoint(value: Any) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


async def _memory_health(port: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
            response = await client.get(f"http://127.0.0.1:{port}/health")
        payload = response.json() if response.is_success else {}
        healthy = (
            response.is_success
            and payload.get("app") == "N.E.K.O"
            and payload.get("service") == "memory"
            and payload.get("status") == "ok"
        )
        return {
            "healthy": healthy,
            "http_status": response.status_code,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "error": None if healthy else "unexpected_health_response",
        }
    except (httpx.HTTPError, OSError, ValueError, json.JSONDecodeError):
        return {
            "healthy": False,
            "http_status": None,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "error": "unavailable",
        }


async def preflight() -> dict[str, Any]:
    """Inspect provider readiness without returning secrets or making paid calls."""

    manager = get_config_manager()
    conversation = await manager.aget_model_api_config("conversation")
    engine = ConversationEngine()
    character_name, base_prompt = await engine.character()
    with tempfile.TemporaryDirectory(prefix="neko-provider-preflight-") as temporary:
        speech = SpeechService(Path(temporary) / "speech")
        try:
            worker, api_key, provider_key, voice_id = await asyncio.to_thread(
                speech._resolve_route
            )
            tts_configured = (
                speech.configured
                and worker is not dummy_tts_worker
                and provider_key is not None
            )
            tts = {
                "configured": tts_configured,
                "provider": str(provider_key or "none"),
                "api_key_present": bool(str(api_key or "").strip()),
                "voice_present": bool(str(voice_id or "").strip()),
            }
        except Exception:
            tts = {
                "configured": False,
                "provider": "unresolved",
                "api_key_present": False,
                "voice_present": False,
            }
        finally:
            await speech.shutdown()
    base_url = conversation.get("base_url")
    conversation_result = {
        "configured": bool(conversation.get("model")) and _safe_endpoint(base_url),
        "provider": str(
            conversation.get("provider_type")
            or conversation.get("api_type")
            or "unspecified"
        ),
        "api_key_present": bool(str(conversation.get("api_key") or "").strip()),
        "model_present": bool(str(conversation.get("model") or "").strip()),
        "endpoint_valid": _safe_endpoint(base_url),
        "character_present": bool(character_name.strip()),
        "persona_prompt_present": bool(base_prompt.strip()),
    }
    memory = await _memory_health(MEMORY_SERVER_PORT)
    ready = bool(
        conversation_result["configured"] and memory["healthy"] and tts["configured"]
    )
    return {
        "mode": "preflight",
        "checked_at": _utc_now(),
        "paid_calls_made": False,
        "secrets_redacted": True,
        "ready_for_live_acceptance": ready,
        "conversation": conversation_result,
        "memory": memory,
        "tts": tts,
    }


def _verify_wav(path: Path) -> dict[str, int]:
    if not path.is_file() or path.stat().st_size <= 44:
        raise ProviderAcceptanceError("TTS output is missing or empty")
    try:
        with wave.open(str(path), "rb") as audio:
            metadata = {
                "channels": audio.getnchannels(),
                "sample_width": audio.getsampwidth(),
                "sample_rate": audio.getframerate(),
                "frames": audio.getnframes(),
            }
    except (OSError, wave.Error) as exc:
        raise ProviderAcceptanceError(f"TTS output is not a valid WAV: {exc}") from exc
    if metadata["channels"] != 1 or metadata["sample_width"] != 2 or metadata["frames"] <= 0:
        raise ProviderAcceptanceError(f"unexpected WAV metadata: {metadata}")
    return metadata


async def run_live_acceptance(
    *,
    engine: Any | None = None,
    memory: Any | None = None,
    speech: Any | None = None,
) -> dict[str, Any]:
    """Run one charged, isolated turn and erase its temporary memory subject."""

    owned_speech_root: tempfile.TemporaryDirectory[str] | None = None
    if engine is None:
        engine = ConversationEngine()
    if memory is None:
        memory = MemoryFacade()
    if speech is None:
        owned_speech_root = tempfile.TemporaryDirectory(prefix="neko-provider-live-")
        speech = SpeechService(Path(owned_speech_root.name) / "speech")

    acceptance_id = f"acceptance_{uuid4().hex}"
    visitor = Visitor(id=acceptance_id, display_name="Provider Acceptance")
    message = RoomMessage(
        id=f"msg_{uuid4().hex}",
        room_id="acceptance",
        room_seq=1,
        author_type="visitor",
        author_id=visitor.id,
        display_name=visitor.display_name,
        content="这是隔离的供应商验收消息，请简短确认服务可用。",
        created_at=utc_now(),
    )
    report: dict[str, Any] = {
        "mode": "live",
        "started_at": _utc_now(),
        "paid_calls_made": True,
        "secrets_redacted": True,
        "acceptance_subject": acceptance_id,
        "conversation": {"passed": False},
        "memory": {"passed": False, "cleanup_passed": False},
        "tts": {"passed": False},
    }
    memory_touched = False
    speech_path: Path | None = None
    character_name = "NEKO"
    try:
        deltas = 0
        first_delta_ms: int | None = None
        llm_started = time.monotonic()

        async def on_delta(text: str) -> None:
            nonlocal deltas, first_delta_ms
            if text:
                deltas += 1
                if first_delta_ms is None:
                    first_delta_ms = round((time.monotonic() - llm_started) * 1000)

        character_name, assistant_text = await asyncio.wait_for(
            engine.generate(
                room_context="[供应商验收] 不使用长期事实，只回复一句简短中文。",
                user_text=message.content,
                on_delta=on_delta,
            ),
            timeout=180.0,
        )
        if not str(assistant_text).strip() or deltas <= 0:
            raise ProviderAcceptanceError("LLM returned no streamed text")
        report["conversation"] = {
            "passed": True,
            "first_delta_ms": first_delta_ms,
            "total_ms": round((time.monotonic() - llm_started) * 1000),
            "delta_count": deltas,
            "response_chars": len(assistant_text),
        }

        memory_started = time.monotonic()
        memory_touched = True
        await asyncio.wait_for(
            memory.record_interaction(
                character_name=character_name,
                room_id="acceptance",
                visitor=visitor,
                user_message=message,
                assistant_text=assistant_text,
            ),
            timeout=60.0,
        )
        context = await asyncio.wait_for(
            memory.build_context(
                character_name=character_name,
                room_id="acceptance",
                target_visitor=visitor,
                recent_messages=[message],
            ),
            timeout=15.0,
        )
        if not str(context).strip():
            raise ProviderAcceptanceError("Memory returned an empty scoped context")
        report["memory"].update(
            {"passed": True, "round_trip_ms": round((time.monotonic() - memory_started) * 1000)}
        )

        tts_started = time.monotonic()
        speech_payload = await asyncio.wait_for(
            speech.synthesize(assistant_text[:200]), timeout=120.0
        )
        filename = Path(str(speech_payload.get("url") or "")).name
        speech_path = Path(speech.audio_root) / filename
        wav = _verify_wav(speech_path)
        report["tts"] = {
            "passed": True,
            "total_ms": round((time.monotonic() - tts_started) * 1000),
            "provider": str(speech_payload.get("provider") or "unknown"),
            **wav,
        }
    except Exception as exc:
        report["error_code"] = type(exc).__name__
    finally:
        if memory_touched:
            try:
                await asyncio.wait_for(
                    memory.forget_visitor(
                        character_name=character_name,
                        room_id="acceptance",
                        visitor_id=visitor.id,
                    ),
                    timeout=60.0,
                )
                report["memory"]["cleanup_passed"] = True
            except Exception as exc:
                report["memory"]["cleanup_error_code"] = type(exc).__name__
        try:
            await speech.shutdown()
        except Exception as exc:
            report["tts"]["shutdown_error_code"] = type(exc).__name__
        if speech_path is not None:
            speech_path.unlink(missing_ok=True)
        if owned_speech_root is not None:
            owned_speech_root.cleanup()
    report["completed_at"] = _utc_now()
    report["passed"] = bool(
        report["conversation"]["passed"]
        and report["memory"]["passed"]
        and report["memory"]["cleanup_passed"]
        and report["tts"]["passed"]
    )
    return report


def _write_report(path: Path | None, report: dict[str, Any]) -> None:
    if path is None:
        return
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight_parser = subparsers.add_parser("preflight", help="read-only readiness check")
    preflight_parser.add_argument("--output", type=Path)
    live_parser = subparsers.add_parser("live", help="make real paid provider calls")
    live_parser.add_argument("--acknowledge", required=True)
    live_parser.add_argument("--output", type=Path, required=True)
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    if args.command == "preflight":
        report = await preflight()
    else:
        if args.acknowledge != ACKNOWLEDGEMENT:
            raise ProviderAcceptanceError(
                f"live mode requires --acknowledge {ACKNOWLEDGEMENT}"
            )
        readiness = await preflight()
        if not readiness["ready_for_live_acceptance"]:
            raise ProviderAcceptanceError("provider preflight is not ready")
        report = await run_live_acceptance()
        report["preflight"] = readiness
    _write_report(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if args.command == "preflight":
        return 0 if report["ready_for_live_acceptance"] else 1
    return 0 if report["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_async_main(args))
    except ProviderAcceptanceError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Verify the provider acceptance workflow with deterministic fake providers."""

from __future__ import annotations

import asyncio
import argparse
import shutil
import tempfile
import wave
from pathlib import Path

from provider_acceptance import (
    ACKNOWLEDGEMENT,
    ProviderAcceptanceError,
    _async_main,
    _parser,
    run_live_acceptance,
)


class FakeEngine:
    async def generate(self, *, room_context, user_text, on_delta):
        assert "供应商验收" in room_context
        assert "隔离" in user_text
        await on_delta("服务")
        await on_delta("正常")
        return "NEKO", "服务正常"


class FakeMemory:
    def __init__(self):
        self.recorded = False
        self.context_built = False
        self.forgotten = False

    async def record_interaction(self, **kwargs):
        assert kwargs["room_id"] == "acceptance"
        assert kwargs["visitor"].id.startswith("acceptance_")
        self.recorded = True

    async def build_context(self, **kwargs):
        assert self.recorded
        assert kwargs["target_visitor"].id.startswith("acceptance_")
        self.context_built = True
        return "[隔离验收上下文]"

    async def forget_visitor(self, **kwargs):
        assert self.recorded
        assert kwargs["visitor_id"].startswith("acceptance_")
        self.forgotten = True
        return {"forgotten": True}


class FakeSpeech:
    def __init__(self, audio_root: Path):
        self.audio_root = audio_root
        self.audio_root.mkdir(parents=True)
        self.shutdown_called = False

    async def synthesize(self, text: str):
        assert text == "服务正常"
        destination = self.audio_root / "provider-acceptance.wav"
        with wave.open(str(destination), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(b"\x01\x00" * 480)
        return {
            "url": "/speech-assets/provider-acceptance.wav",
            "provider": "fake-tts",
        }

    async def shutdown(self):
        self.shutdown_called = True


class FailingSpeech(FakeSpeech):
    async def synthesize(self, text: str):
        raise RuntimeError("deliberate fake TTS outage")


async def verify() -> None:
    temporary = Path(tempfile.mkdtemp(prefix="provider-acceptance-verify-"))
    memory = FakeMemory()
    speech = FakeSpeech(temporary / "speech")
    try:
        report = await run_live_acceptance(
            engine=FakeEngine(), memory=memory, speech=speech
        )
        assert report["passed"] is True
        assert report["paid_calls_made"] is True
        assert report["secrets_redacted"] is True
        assert report["conversation"]["delta_count"] == 2
        assert report["conversation"]["response_chars"] == 4
        assert report["memory"]["passed"] is True
        assert report["memory"]["cleanup_passed"] is True
        assert report["tts"]["passed"] is True
        assert report["tts"]["sample_rate"] == 48000
        assert memory.recorded and memory.context_built and memory.forgotten
        assert speech.shutdown_called
        assert not list(speech.audio_root.glob("*.wav"))

        parser = _parser()
        parsed = parser.parse_args(
            [
                "live",
                "--acknowledge",
                ACKNOWLEDGEMENT,
                "--output",
                str(temporary / "evidence.json"),
            ]
        )
        assert parsed.command == "live"
        assert parsed.acknowledge == ACKNOWLEDGEMENT
        assert parsed.output.name == "evidence.json"

        failure_memory = FakeMemory()
        failing_speech = FailingSpeech(temporary / "failing-speech")
        failed = await run_live_acceptance(
            engine=FakeEngine(), memory=failure_memory, speech=failing_speech
        )
        assert failed["passed"] is False
        assert failed["error_code"] == "RuntimeError"
        assert failed["memory"]["cleanup_passed"] is True
        assert failure_memory.forgotten and failing_speech.shutdown_called

        try:
            await _async_main(
                argparse.Namespace(
                    command="live",
                    acknowledge="WRONG_ACKNOWLEDGEMENT",
                    output=temporary / "must-not-exist.json",
                )
            )
        except ProviderAcceptanceError as exc:
            assert ACKNOWLEDGEMENT in str(exc)
        else:
            raise AssertionError("live provider calls were not blocked by acknowledgement")
        assert not (temporary / "must-not-exist.json").exists()
        print(
            "provider acceptance verification passed: streamed LLM, scoped Memory "
            "cleanup, WAV validation, redacted evidence, and explicit cost acknowledgement"
        )
    finally:
        shutil.rmtree(temporary, ignore_errors=False)


if __name__ == "__main__":
    asyncio.run(verify())

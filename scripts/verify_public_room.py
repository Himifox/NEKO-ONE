"""Deterministic public-room smoke verification without a real model call."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.verification_postgres import connect, database_url, reset_public_tables


def _drain_until(websocket, wanted: str, limit: int = 30) -> list[dict]:
    seen: list[dict] = []
    for _ in range(limit):
        event = websocket.receive_json()
        seen.append(event)
        if event.get("type") == wanted:
            return seen
    raise AssertionError((wanted, [event.get("type") for event in seen]))


def _wait_for_dependency(
    service,
    name: str,
    status: str,
    timeout: float = 2.0,
    error_code: str | None = None,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dependency = service.dependency_snapshot()[name]
        if dependency["status"] == status and (
            error_code is None or dependency["error_code"] == error_code
        ):
            return dependency
        time.sleep(0.01)
    raise AssertionError((name, status, service.dependency_snapshot()[name]))


def main() -> None:
    reset_public_tables()
    workspace_var = Path.cwd() / "var"
    workspace_var.mkdir(exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix="public-room-verify-", dir=workspace_var))
    os.environ["NEKO_PUBLIC_DATA_DIR"] = str(data_dir)
    os.environ["NEKO_PUBLIC_ALLOW_LEGACY_MEMORY"] = "0"
    os.environ["NEKO_PUBLIC_ADMIN_PASSWORD"] = "verification-admin-password"
    os.environ["NEKO_PUBLIC_LIVE2D_MODEL_NAME"] = ""
    os.environ["NEKO_PUBLIC_LIVE2D_MODEL_FILE"] = ""
    (data_dir / "live2d").mkdir(parents=True, exist_ok=True)
    (data_dir / "live2d" / "private-note.txt").write_text(
        "must not be public", encoding="utf-8"
    )
    model_root = data_dir / "live2d" / "verification-model"
    model_root.mkdir()
    (model_root / "verification.moc3").write_bytes(b"MOC3 verification")
    (model_root / "texture.png").write_bytes(b"PNG verification")
    (model_root / "private-note.txt").write_text("must stay private", encoding="utf-8")
    (model_root / "verification.model3.json").write_text(
        json.dumps(
            {
                "Version": 3,
                "FileReferences": {
                    "Moc": "verification.moc3",
                    "Textures": ["texture.png"],
                },
            }
        ),
        encoding="utf-8",
    )
    speech_root = data_dir / "speech"
    speech_root.mkdir(parents=True, exist_ok=True)
    public_speech_name = f"speech_{'a' * 32}.wav"
    (speech_root / public_speech_name).write_bytes(b"RIFF-verification")
    (speech_root / "private-note.txt").write_text(
        "must not be public", encoding="utf-8"
    )
    (speech_root / f"{public_speech_name}.tmp").write_bytes(b"partial")

    from fastapi.testclient import TestClient

    from app.public_room_server.web_app import app
    from main_logic.room.service import PublicRoomService

    async def fake_generate(*, room_context, user_text, on_delta):
        assert "公共房间" in room_context
        assert "NEKO 你好" in user_text
        await on_delta("你")
        await on_delta("好")
        return "NEKO", "你好"

    async def fake_memory_context(**_kwargs):
        return "[公共房间规则]\n验证用隔离上下文"

    async def fake_memory_write(**_kwargs):
        return None

    async def fake_speech(_text):
        return {
            "speech_id": "speech-smoke",
            "url": "/speech-assets/speech-smoke.wav",
            "content_type": "audio/wav",
            "sample_rate": 48000,
            "provider": "verification-private-provider",
        }

    try:
        with TestClient(app) as client:
            avatar = client.get("/api/v1/avatar")
            assert avatar.status_code == 200
            assert avatar.json()["enabled"] is False
            assert avatar.json()["status"] == "not_configured"
            assert client.get("/live2d-assets/private-note.txt").status_code == 404
            assert client.get(f"/speech-assets/{public_speech_name}").status_code == 200
            assert client.get("/speech-assets/private-note.txt").status_code == 404
            assert client.get(
                f"/speech-assets/{public_speech_name}.tmp"
            ).status_code == 404
            real_engine_readiness = app.state.room_service.engine.readiness_snapshot
            real_memory_health = app.state.room_service.memory.health_snapshot
            real_storage_readiness = app.state.room_service.store.readiness_snapshot

            async def fake_engine_readiness():
                return {
                    "configured": True,
                    "endpoint_valid": True,
                    "model_present": True,
                    "persona_present": True,
                    "error_code": None,
                }

            async def fake_memory_health():
                return {"healthy": False, "status": "degraded", "error_code": "unavailable"}

            app.state.room_service.engine.readiness_snapshot = fake_engine_readiness
            app.state.room_service.memory.health_snapshot = fake_memory_health
            ready = client.get("/api/v1/health/ready")
            assert ready.status_code == 200
            readiness = ready.json()
            assert readiness["ok"] is True
            assert readiness["storage"]["schema_version"] == 1
            assert readiness["storage"]["writable"] is True
            assert readiness["storage"]["disk_space_ok"] is True
            assert "free_mib" not in readiness["storage"]
            assert "minimum_free_mib" not in readiness["storage"]
            assert readiness["memory"]["status"] == "degraded"
            assert readiness["optional"]["live2d"] is False

            async def failing_storage_readiness(*, minimum_free_mib):
                return {
                    "ok": False,
                    "schema_version": 1,
                    "writable": False,
                    "disk_space_ok": True,
                    "error_code": "postgres_unavailable",
                }

            app.state.room_service.store.readiness_snapshot = failing_storage_readiness
            not_ready = client.get("/api/v1/health/ready")
            assert not_ready.status_code == 503
            assert not_ready.json()["ok"] is False
            assert not_ready.json()["storage"]["error_code"] == "postgres_unavailable"
            app.state.room_service.store.readiness_snapshot = real_storage_readiness
            app.state.room_service.engine.readiness_snapshot = real_engine_readiness
            app.state.room_service.memory.health_snapshot = real_memory_health
            real_memory_build_context = app.state.room_service.memory.build_context
            app.state.room_service.engine.generate = fake_generate
            app.state.room_service.memory.build_context = fake_memory_context
            app.state.room_service.memory.record_interaction = fake_memory_write
            app.state.room_service.memory.record_mentions = fake_memory_write
            app.state.room_service.speech._disabled = False
            app.state.room_service.speech.synthesize = fake_speech
            session = client.post("/api/v1/session/guest", json={})
            assert session.status_code == 200
            assert session.json()["visitor"]["id"].startswith("vis_")

            with client.websocket_connect("/ws/rooms/main?after_seq=0") as ws1:
                _drain_until(ws1, "presence.updated")
                with client.websocket_connect("/ws/rooms/main?after_seq=0") as ws2:
                    _drain_until(ws2, "presence.updated")
                    ws1.send_json(
                        {
                            "type": "chat.send",
                            "request_id": "req-smoke",
                            "payload": {"text": "NEKO 你好"},
                        }
                    )
                    events1 = _drain_until(ws1, "stream.completed")
                    events2 = _drain_until(ws2, "stream.completed")
                    types1 = [event.get("type") for event in events1]
                    types2 = [event.get("type") for event in events2]
                    assert types1.count("message.created") == 2
                    assert types2.count("message.created") == 2
                    assert "chat.accepted" in types1
                    assert "chat.accepted" not in types2
                    assert types1.count("stream.delta") == 2
                    assert types2.count("stream.delta") == 2
                    public_events = [
                        event
                        for event in events1
                        if event.get("type")
                        in {"message.created", "turn.started", "stream.started"}
                    ]
                    serialized_public_events = json.dumps(
                        public_events, ensure_ascii=False
                    )
                    for private_token in (
                        "author_id",
                        "metadata",
                        "target_visitor_id",
                        "source_message_ids",
                        "memory_scope",
                        "group_participant:web:main",
                    ):
                        assert private_token not in serialized_public_events
                    room_seqs = [
                        event["room_seq"]
                        for event in events1
                        if isinstance(event.get("room_seq"), int)
                    ]
                    assert room_seqs == sorted(room_seqs)
                    assert room_seqs[-1] == 3

            history = client.get("/api/v1/rooms/main/messages").json()["messages"]
            assert [message["content"] for message in history] == ["NEKO 你好", "你好"]
            assert all("author_id" not in message for message in history)
            assert all("metadata" not in message for message in history)
            with connect() as connection:
                internal_metadata = connection.execute(
                    """
                    SELECT metadata_json FROM messages
                    WHERE author_type = 'neko'
                    ORDER BY room_seq ASC LIMIT 1
                    """
                ).fetchone()["metadata_json"]
            assert internal_metadata["target_visitor_id"].startswith("vis_")
            assert "visitor_scope" in internal_metadata["memory_scope"]

            with client.websocket_connect("/ws/rooms/main?after_seq=0") as cold_start:
                assert cold_start.receive_json()["type"] == "session.ready"
                snapshot = cold_start.receive_json()
                assert snapshot["type"] == "room.snapshot"
                assert snapshot["room_seq"] == 3
                assert [
                    message["content"] for message in snapshot["payload"]["messages"]
                ] == ["NEKO 你好", "你好"]
                assert snapshot["payload"]["last_room_seq"] == 3
                assert cold_start.receive_json()["type"] == "presence.updated"

            with client.websocket_connect("/ws/rooms/main?after_seq=1") as reconnected:
                assert reconnected.receive_json()["type"] == "session.ready"
                replayed_turn = reconnected.receive_json()
                replayed_reply = reconnected.receive_json()
                assert (replayed_turn["type"], replayed_turn["room_seq"]) == (
                    "turn.started",
                    2,
                )
                assert (replayed_reply["type"], replayed_reply["room_seq"]) == (
                    "message.created",
                    3,
                )
                assert reconnected.receive_json()["type"] == "presence.updated"
                reconnected.send_json(
                    {
                        "type": "chat.send",
                        "request_id": "req-smoke",
                        "payload": {"text": "NEKO 你好"},
                    }
                )
                duplicate = reconnected.receive_json()
                assert duplicate["type"] == "chat.accepted"
                assert duplicate["payload"]["duplicate"] is True

            admin_login = client.post(
                "/api/v1/admin/session",
                json={"password": "verification-admin-password"},
            )
            assert admin_login.status_code == 200
            csrf = admin_login.json()["csrf"]
            headers = {"X-NEKO-CSRF": csrf}
            admin_state = client.get("/api/v1/admin/state")
            assert admin_state.status_code == 200
            assert admin_state.json()["totals"]["messages"] == 2
            assert admin_state.json()["avatar"]["models"] == [
                {
                    "model_name": "verification-model",
                    "model_file": "verification.model3.json",
                    "valid": True,
                    "active": False,
                }
            ]
            activate_avatar = client.put(
                "/api/v1/admin/avatar",
                json={
                    "enabled": True,
                    "model_name": "verification-model",
                    "model_file": "verification.model3.json",
                },
                headers=headers,
            )
            assert activate_avatar.status_code == 200
            assert activate_avatar.json()["current"]["status"] == "ready"
            assert client.get("/api/v1/avatar").json()["enabled"] is True
            assert client.get(
                "/live2d-assets/verification-model/verification.model3.json"
            ).status_code == 200
            assert client.get(
                "/live2d-assets/verification-model/private-note.txt"
            ).status_code == 404
            rejected_avatar = client.put(
                "/api/v1/admin/avatar",
                json={
                    "enabled": True,
                    "model_name": "verification-model",
                    "model_file": "missing.model3.json",
                },
                headers=headers,
            )
            assert rejected_avatar.status_code == 400
            assert client.get("/api/v1/avatar").json()["enabled"] is True
            disable_avatar = client.put(
                "/api/v1/admin/avatar",
                json={"enabled": False},
                headers=headers,
            )
            assert disable_avatar.status_code == 200
            assert client.get("/api/v1/avatar").json()["enabled"] is False
            assert client.get(
                "/live2d-assets/verification-model/verification.model3.json"
            ).status_code == 404
            limits = client.put(
                "/api/v1/admin/limits",
                json={
                    "max_message_chars": 1800,
                    "messages_per_window": 20,
                    "window_seconds": 12,
                },
                headers=headers,
            )
            assert limits.status_code == 200
            assert limits.json()["limits"]["messages_per_window"] == 20

            async def failing_generate(**_kwargs):
                await asyncio.Event().wait()

            app.state.room_service.engine.generate = failing_generate
            app.state.room_service.llm_timeout_seconds = 0.05
            room = client.get("/api/v1/rooms/main").json()
            with client.websocket_connect(
                f"/ws/rooms/main?after_seq={room['last_seq']}"
            ) as failure_ws:
                _drain_until(failure_ws, "presence.updated")
                failure_ws.send_json(
                    {
                        "type": "chat.send",
                        "request_id": "req-llm-failure",
                        "payload": {"text": "NEKO 你好"},
                    }
                )
                llm_failure = _drain_until(failure_ws, "stream.failed")
                assert llm_failure[-1]["payload"]["code"] == "generation_failed"
                assert any(
                    event.get("type") == "turn.interrupted"
                    and event.get("payload", {}).get("reason")
                    == "generation_failed"
                    for event in llm_failure
                )
                dependency = _wait_for_dependency(
                    app.state.room_service, "llm", "degraded"
                )
                assert dependency["error_code"] == "timeout"

                app.state.room_service.engine.generate = fake_generate
                app.state.room_service.llm_timeout_seconds = 120
                failure_ws.send_json(
                    {
                        "type": "chat.send",
                        "request_id": "req-llm-recovery",
                        "payload": {"text": "NEKO 你好"},
                    }
                )
                _drain_until(failure_ws, "stream.completed")
                _drain_until(failure_ws, "speech.ready")
                _wait_for_dependency(app.state.room_service, "llm", "ready")

            memory = app.state.room_service.memory
            memory.memory_server_port = 1
            memory.build_context = real_memory_build_context
            memory_write_failures: list[str] = []

            async def failing_memory_write(**_kwargs):
                memory_write_failures.append("failed")
                raise RuntimeError("verification Memory outage")

            memory.record_interaction = failing_memory_write
            room = client.get("/api/v1/rooms/main").json()
            with client.websocket_connect(
                f"/ws/rooms/main?after_seq={room['last_seq']}"
            ) as memory_ws:
                _drain_until(memory_ws, "presence.updated")
                memory_ws.send_json(
                    {
                        "type": "chat.send",
                        "request_id": "req-memory-failure",
                        "payload": {"text": "NEKO 你好"},
                    }
                )
                memory_events = _drain_until(memory_ws, "stream.completed")
                assert any(
                    event.get("type") == "message.created"
                    and event.get("payload", {}).get("author_type") == "neko"
                    for event in memory_events
                )
                dependency = _wait_for_dependency(
                    app.state.room_service,
                    "memory",
                    "degraded",
                    error_code="RuntimeError",
                )
                assert dependency["error_code"] == "RuntimeError"
                assert len(memory_write_failures) == (
                    app.state.room_service.memory_write_attempts
                )
                _drain_until(memory_ws, "speech.ready")

                memory.build_context = fake_memory_context
                memory.context_degraded = False
                memory.context_error_code = None
                memory.record_interaction = fake_memory_write
                memory.record_mentions = fake_memory_write
                memory_ws.send_json(
                    {
                        "type": "chat.send",
                        "request_id": "req-memory-recovery",
                        "payload": {"text": "NEKO 你好"},
                    }
                )
                _drain_until(memory_ws, "stream.completed")
                _drain_until(memory_ws, "speech.ready")
                _wait_for_dependency(app.state.room_service, "memory", "ready")

            speech_failures: list[str] = []

            async def failing_speech(_text):
                speech_failures.append("failed")
                raise RuntimeError("verification TTS outage")

            app.state.room_service.speech.synthesize = failing_speech
            room = client.get("/api/v1/rooms/main").json()
            with client.websocket_connect(
                f"/ws/rooms/main?after_seq={room['last_seq']}"
            ) as speech_ws:
                _drain_until(speech_ws, "presence.updated")
                speech_ws.send_json(
                    {
                        "type": "chat.send",
                        "request_id": "req-tts-failure",
                        "payload": {"text": "NEKO 你好"},
                    }
                )
                speech_events = _drain_until(speech_ws, "stream.completed")
                assert any(
                    event.get("type") == "message.created"
                    and event.get("payload", {}).get("author_type") == "neko"
                    for event in speech_events
                )
                _drain_until(speech_ws, "speech.failed")
                dependency = _wait_for_dependency(
                    app.state.room_service, "tts", "degraded"
                )
                assert dependency["error_code"] == "RuntimeError"
                assert len(speech_failures) == app.state.room_service.tts_attempts

                app.state.room_service.speech.synthesize = fake_speech
                speech_ws.send_json(
                    {
                        "type": "chat.send",
                        "request_id": "req-tts-recovery",
                        "payload": {"text": "NEKO 你好"},
                    }
                )
                _drain_until(speech_ws, "stream.completed")
                ready_events = _drain_until(speech_ws, "speech.ready")
                ready_event = next(
                    event for event in ready_events if event.get("type") == "speech.ready"
                )
                assert "provider" not in ready_event["payload"]
                assert "verification-private-provider" not in json.dumps(ready_event)
                _wait_for_dependency(app.state.room_service, "tts", "ready")

            dependency_state = client.get("/api/v1/admin/state").json()[
                "dependencies"
            ]
            assert set(dependency_state) == {"llm", "memory", "tts"}
            assert all(
                dependency_state[name]["status"] == "ready"
                for name in dependency_state
            )

            controls = client.put(
                "/api/v1/admin/room-controls",
                json={
                    "paused": False,
                    "read_only": True,
                    "proactive_enabled": False,
                },
                headers=headers,
            )
            assert controls.status_code == 200
            assert controls.json()["controls"]["read_only"] is True
            room = client.get("/api/v1/rooms/main").json()
            assert room["controls"] == controls.json()["controls"]
            with client.websocket_connect(
                f"/ws/rooms/main?after_seq={room['last_seq']}"
            ) as read_only_ws:
                _drain_until(read_only_ws, "presence.updated")
                read_only_ws.send_json(
                    {
                        "type": "chat.send",
                        "request_id": "req-read-only",
                        "payload": {"text": "NEKO 你好"},
                    }
                )
                rejected = _drain_until(read_only_ws, "command.rejected")[-1]
                assert rejected["payload"]["code"] == "room_read_only"

            paused = client.put(
                "/api/v1/admin/room-controls",
                json={
                    "paused": True,
                    "read_only": False,
                    "proactive_enabled": False,
                },
                headers=headers,
            )
            assert paused.status_code == 200
            assert paused.json()["controls"]["paused"] is True
            room = client.get("/api/v1/rooms/main").json()
            with client.websocket_connect(
                f"/ws/rooms/main?after_seq={room['last_seq']}"
            ) as paused_ws:
                _drain_until(paused_ws, "presence.updated")
                paused_ws.send_json(
                    {
                        "type": "chat.send",
                        "request_id": "req-paused",
                        "payload": {"text": "NEKO 你好"},
                    }
                )
                rejected = _drain_until(paused_ws, "command.rejected")[-1]
                assert rejected["payload"]["code"] == "room_paused"

            proactive = client.put(
                "/api/v1/admin/room-controls",
                json={
                    "paused": False,
                    "read_only": False,
                    "proactive_enabled": True,
                },
                headers=headers,
            )
            assert proactive.status_code == 200
            assert proactive.json()["controls"]["proactive_enabled"] is True
            disabled_proactive = client.put(
                "/api/v1/admin/room-controls",
                json={
                    "paused": False,
                    "read_only": False,
                    "proactive_enabled": False,
                },
                headers=headers,
            )
            assert disabled_proactive.status_code == 200

            async def slow_generate(*, room_context, user_text, on_delta):
                assert "公共房间" in room_context
                await on_delta("等")
                await asyncio.Event().wait()

            app.state.room_service.engine.generate = slow_generate
            room = client.get("/api/v1/rooms/main").json()
            with client.websocket_connect(
                f"/ws/rooms/main?after_seq={room['last_seq']}"
            ) as cancellable_ws:
                _drain_until(cancellable_ws, "presence.updated")
                cancellable_ws.send_json(
                    {
                        "type": "chat.send",
                        "request_id": "req-cancel",
                        "payload": {"text": "NEKO 你好"},
                    }
                )
                _drain_until(cancellable_ws, "stream.delta")
                active_room = client.get("/api/v1/rooms/main").json()
                with client.websocket_connect(
                    f"/ws/rooms/main?after_seq={active_room['last_seq']}"
                ) as snapshot_ws:
                    ready = snapshot_ws.receive_json()
                    snapshot = snapshot_ws.receive_json()
                    assert ready["type"] == "session.ready"
                    assert snapshot["type"] == "stream.snapshot"
                    assert snapshot["payload"]["text"] == "等"
                    assert "target_visitor_id" not in snapshot["payload"]
                    assert "source_message_ids" not in snapshot["payload"]
                    assert snapshot_ws.receive_json()["type"] == "presence.updated"

                    cancelled = client.post(
                        "/api/v1/admin/generation/cancel", headers=headers
                    )
                    assert cancelled.status_code == 200
                    assert cancelled.json()["cancelled"] is True
                    cancelled_events = _drain_until(
                        cancellable_ws, "stream.failed"
                    )
                    assert any(
                        event.get("type") == "turn.interrupted"
                        and event.get("payload", {}).get("reason")
                        == "admin_cancelled"
                        for event in cancelled_events
                    )
                    assert (
                        cancelled_events[-1]["payload"]["code"]
                        == "admin_cancelled"
                    )
                    snapshot_terminal = _drain_until(
                        snapshot_ws, "stream.failed"
                    )
                    assert not any(
                        event.get("type") == "stream.delta"
                        for event in snapshot_terminal
                    )

                app.state.room_service.engine.generate = fake_generate
                cancellable_ws.send_json(
                    {
                        "type": "chat.send",
                        "request_id": "req-after-cancel",
                        "payload": {"text": "NEKO 你好"},
                    }
                )
                resumed_events = _drain_until(
                    cancellable_ws, "stream.completed"
                )
                assert any(
                    event.get("type") == "message.created"
                    and event.get("payload", {}).get("content") == "你好"
                    for event in resumed_events
                )

            assistant_id = history[-1]["id"]
            hidden = client.put(
                f"/api/v1/admin/messages/{assistant_id}/status",
                json={"status": "hidden"},
                headers=headers,
            )
            assert hidden.status_code == 200
            visible_history = client.get("/api/v1/rooms/main/messages").json()["messages"]
            assert assistant_id not in {
                message["id"] for message in visible_history
            }
            restored = client.put(
                f"/api/v1/admin/messages/{assistant_id}/status",
                json={"status": "visible"},
                headers=headers,
            )
            assert restored.status_code == 200
            restored_history = client.get("/api/v1/rooms/main/messages").json()[
                "messages"
            ]
            assert assistant_id in {message["id"] for message in restored_history}
            with connect() as connection:
                moderation_payloads = [
                    row["payload_json"]
                    for row in connection.execute(
                        """
                        SELECT payload_json FROM room_events
                        WHERE type = 'message.moderated'
                          AND payload_json->>'message_id' = %s
                        ORDER BY room_seq DESC LIMIT 2
                        """,
                        (assistant_id,),
                    ).fetchall()
                ]
            assert moderation_payloads[1]["status"] == "hidden"
            assert moderation_payloads[1]["message"] is None
            assert moderation_payloads[0]["status"] == "visible"
            assert moderation_payloads[0]["message"]["id"] == assistant_id
            visitor_id = session.json()["visitor"]["id"]
            banned = client.put(
                f"/api/v1/admin/visitors/{visitor_id}/status",
                json={"status": "banned"},
                headers=headers,
            )
            assert banned.status_code == 200

            persisted_controls = client.put(
                "/api/v1/admin/room-controls",
                json={
                    "paused": False,
                    "read_only": True,
                    "proactive_enabled": False,
                },
                headers=headers,
            )
            assert persisted_controls.status_code == 200

            retention = client.put(
                "/api/v1/admin/retention",
                json={
                    "message_days": 1,
                    "visitor_days": 1,
                    "audit_days": 7,
                    "speech_hours": 1,
                    "cleanup_interval_minutes": 5,
                },
                headers=headers,
            )
            assert retention.status_code == 200
            assert retention.json()["retention"]["visitor_days"] == 1

            forgotten_visitors: list[str] = []

            async def fake_forget_visitor(**kwargs):
                forgotten_visitors.append(kwargs["visitor_id"])
                return {"ok": True}

            app.state.room_service.memory.forget_visitor = fake_forget_visitor
            expired_audio = app.state.room_service.speech.audio_root / "expired.wav"
            recent_audio = app.state.room_service.speech.audio_root / "recent.wav"
            expired_audio.write_bytes(b"expired")
            recent_audio.write_bytes(b"recent")
            os.utime(expired_audio, (946684800, 946684800))

            old_timestamp = "2000-01-01T00:00:00Z"
            with connect() as connection:
                for table, columns in {
                    "messages": ("created_at",),
                    "room_events": ("created_at",),
                    "client_requests": ("created_at",),
                    "turns": ("started_at", "completed_at"),
                    "audit_log": ("created_at",),
                    "visitors": ("last_seen_at",),
                }.items():
                    for column in columns:
                        connection.execute(
                            f"UPDATE {table} SET {column} = %s", (old_timestamp,)
                        )

            cleanup = client.post("/api/v1/admin/retention/run", headers=headers)
            assert cleanup.status_code == 200
            cleanup_result = cleanup.json()["result"]
            assert cleanup_result["counts"]["messages"] >= 1
            assert cleanup_result["counts"]["events"] >= 1
            assert cleanup_result["counts"]["visitors"] == 1
            assert cleanup_result["counts"]["speech_files"] == 1
            assert cleanup_result["memory_forget_failures"] == 0
            assert forgotten_visitors == [visitor_id]
            assert not expired_audio.exists()
            assert recent_audio.exists()
            assert client.get("/api/v1/rooms/main/messages").json()["messages"] == []
            cleaned_state = client.get("/api/v1/admin/state").json()
            assert cleaned_state["totals"]["messages"] == 0
            assert cleaned_state["totals"]["visitors"] == 0

            retry_visitor_id = "vis_retention_retry"
            with connect() as connection:
                connection.execute(
                    """
                    INSERT INTO visitors(
                        id, display_name, status, created_at, last_seen_at
                    ) VALUES(%s, %s, 'active', %s, %s)
                    """,
                    (
                        retry_visitor_id,
                        "Retry Visitor",
                        old_timestamp,
                        old_timestamp,
                    ),
                )

            async def failing_forget_visitor(**_kwargs):
                raise RuntimeError("verification memory outage")

            app.state.room_service.memory.forget_visitor = failing_forget_visitor
            failed_cleanup = client.post(
                "/api/v1/admin/retention/run", headers=headers
            ).json()["result"]
            assert failed_cleanup["memory_forget_failures"] == 1
            assert failed_cleanup["counts"]["visitors"] == 0
            assert client.get("/api/v1/admin/state").json()["totals"]["visitors"] == 1

            app.state.room_service.memory.forget_visitor = fake_forget_visitor
            retried_cleanup = client.post(
                "/api/v1/admin/retention/run", headers=headers
            ).json()["result"]
            assert retried_cleanup["memory_forget_failures"] == 0
            assert retried_cleanup["counts"]["visitors"] == 1
            assert forgotten_visitors == [visitor_id, retry_visitor_id]

            new_session = client.post("/api/v1/session/guest", json={})
            assert new_session.status_code == 200
            cleaned_room = client.get("/api/v1/rooms/main").json()
            assert (
                cleaned_room["oldest_available_seq"]
                == cleaned_room["last_seq"] + 1
            )
            with client.websocket_connect(
                "/ws/rooms/main?after_seq=0"
            ) as reset_ws:
                ready = reset_ws.receive_json()
                assert ready["type"] == "session.ready"
                assert (
                    ready["oldest_available_seq"]
                    == cleaned_room["oldest_available_seq"]
                )
                reset = reset_ws.receive_json()
                assert reset["type"] == "replay.reset"
                assert reset["payload"]["reason"] == "history_expired"
                assert (
                    reset["payload"]["replay_from_seq"]
                    == cleaned_room["last_seq"]
                )
                snapshot = reset_ws.receive_json()
                assert snapshot["type"] == "room.snapshot"
                assert snapshot["room_seq"] == cleaned_room["last_seq"]
                assert snapshot["payload"]["messages"] == []
                assert reset_ws.receive_json()["type"] == "presence.updated"

        async def verify_controls_after_restart() -> None:
            restarted = PublicRoomService(
                database_url=database_url(), data_dir=data_dir
            )

            async def fake_character():
                return "NEKO", "verification persona"

            restarted.engine.character = fake_character
            await restarted.start()
            try:
                assert restarted.controls == {
                    "paused": False,
                    "read_only": True,
                    "proactive_enabled": False,
                }
                assert restarted.retention == {
                    "message_days": 1,
                    "visitor_days": 1,
                    "audit_days": 7,
                    "speech_hours": 1,
                    "cleanup_interval_minutes": 5,
                }
                assert restarted.last_cleanup is not None
            finally:
                await restarted.shutdown()

        asyncio.run(verify_controls_after_restart())
    finally:
        shutil.rmtree(data_dir, ignore_errors=False)

    print("public-room verification passed")


if __name__ == "__main__":
    main()

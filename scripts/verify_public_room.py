"""Deterministic public-room smoke verification without a real model call."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _drain_until(websocket, wanted: str, limit: int = 30) -> list[dict]:
    seen: list[dict] = []
    for _ in range(limit):
        event = websocket.receive_json()
        seen.append(event)
        if event.get("type") == wanted:
            return seen
    raise AssertionError((wanted, [event.get("type") for event in seen]))


def main() -> None:
    workspace_var = Path.cwd() / "var"
    workspace_var.mkdir(exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix="public-room-verify-", dir=workspace_var))
    os.environ["NEKO_PUBLIC_DATA_DIR"] = str(data_dir)
    os.environ["NEKO_PUBLIC_ALLOW_LEGACY_MEMORY"] = "0"
    os.environ["NEKO_PUBLIC_ADMIN_PASSWORD"] = "verification-admin-password"

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
            "provider": "fake",
        }

    try:
        with TestClient(app) as client:
            app.state.room_service.engine.generate = fake_generate
            app.state.room_service.memory.build_context = fake_memory_context
            app.state.room_service.memory.record_interaction = fake_memory_write
            app.state.room_service.memory.record_mentions = fake_memory_write
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
                    room_seqs = [
                        event["room_seq"]
                        for event in events1
                        if isinstance(event.get("room_seq"), int)
                    ]
                    assert room_seqs == sorted(room_seqs)
                    assert room_seqs[-1] == 3

            history = client.get("/api/v1/rooms/main/messages").json()["messages"]
            assert [message["content"] for message in history] == ["NEKO 你好", "你好"]

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
            limits = client.put(
                "/api/v1/admin/limits",
                json={
                    "max_message_chars": 1800,
                    "messages_per_window": 4,
                    "window_seconds": 12,
                },
                headers=headers,
            )
            assert limits.status_code == 200
            assert limits.json()["limits"]["messages_per_window"] == 4

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
                cancelled = client.post(
                    "/api/v1/admin/generation/cancel", headers=headers
                )
                assert cancelled.status_code == 200
                assert cancelled.json()["cancelled"] is True
                cancelled_events = _drain_until(cancellable_ws, "stream.failed")
                assert any(
                    event.get("type") == "turn.interrupted"
                    and event.get("payload", {}).get("reason") == "admin_cancelled"
                    for event in cancelled_events
                )
                assert cancelled_events[-1]["payload"]["code"] == "admin_cancelled"

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

        async def verify_controls_after_restart() -> None:
            restarted = PublicRoomService(
                database_path=data_dir / "public-room.db"
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
            finally:
                await restarted.shutdown()

        asyncio.run(verify_controls_after_restart())
    finally:
        shutil.rmtree(data_dir, ignore_errors=False)

    print("public-room verification passed")


if __name__ == "__main__":
    main()

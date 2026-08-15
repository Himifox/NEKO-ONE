"""Fail when the lean public FastAPI entry accidentally mounts legacy surfaces."""

from __future__ import annotations

import os
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_LEGACY_SOURCES = (
    "utils/capture_bridge.py",
    "utils/cloudsave_autocloud.py",
    "utils/document_parser.py",
    "utils/game_log.py",
    "utils/game_route_state.py",
    "utils/icebreaker_route_state.py",
    "utils/meme_fetcher.py",
    "utils/music_crawlers.py",
    "utils/prompt_state",
    "utils/pyautogui_diagnostics.py",
    "utils/screenshot_utils.py",
    "utils/seven_day_tutorial_state.py",
    "utils/steam_cloud_bundle.py",
    "utils/survey_client.py",
    "utils/twitch_auth.py",
    "utils/voice_clone.py",
    "utils/voice_design.py",
    "utils/web_scraper",
    "utils/workshop_utils.py",
    "config/prompts/prompts_agent.py",
    "config/prompts/prompts_badminton.py",
    "config/prompts/prompts_card_assist.py",
    "config/prompts/prompts_galgame.py",
    "config/prompts/prompts_minigame_route.py",
    "config/prompts/prompts_soccer.py",
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    residual_sources = []
    for relative in FORBIDDEN_LEGACY_SOURCES:
        candidate = ROOT / relative
        if candidate.suffix == ".py" and candidate.is_file():
            residual_sources.append(relative)
        elif candidate.is_dir() and any(candidate.rglob("*.py")):
            residual_sources.append(relative)
    assert not residual_sources, f"legacy source returned to public tree: {residual_sources}"

    from config import DEFAULT_LANLAN_TEMPLATE, DEFAULT_LIVE2D_MODEL_PATH

    assert DEFAULT_LIVE2D_MODEL_PATH == "", "a bundled character model returned"
    default_character = next(iter(DEFAULT_LANLAN_TEMPLATE.values()))
    avatar_defaults = default_character["_reserved"]["avatar"]
    assert not ({"vrm", "mmd", "pngtuber"} & set(avatar_defaults)), (
        "alternate renderer defaults returned"
    )

    with tempfile.TemporaryDirectory(prefix="neko-boundary-") as temporary:
        os.environ["NEKO_PUBLIC_DATA_DIR"] = temporary
        os.environ["NEKO_PUBLIC_LIVE2D_MODEL_NAME"] = ""
        os.environ["NEKO_PUBLIC_LIVE2D_MODEL_FILE"] = ""
        from app.public_room_server.web_app import create_app

        app = create_app()
        paths = {getattr(route, "path", "") for route in app.routes}
        forbidden_fragments = {
            "agent", "plugin", "workshop", "steam", "game", "galgame",
            "capture", "ocr", "vrm", "mmd", "pngtuber", "asr", "microphone",
        }
        violations = sorted(
            path for path in paths
            if any(fragment in path.lower() for fragment in forbidden_fragments)
        )
        assert not violations, f"legacy public routes mounted: {violations}"
        required = {
            "/", "/admin", "/api/v1/health/live", "/api/v1/health/ready",
            "/api/v1/session/guest", "/api/v1/avatar", "/ws/rooms/{room_id}",
            "/api/v1/admin/room-controls",
            "/api/v1/admin/generation/cancel",
            "/api/v1/admin/retention",
            "/api/v1/admin/retention/run",
        }
        missing = sorted(required - paths)
        assert not missing, f"required public routes missing: {missing}"

        frontend = (ROOT / "frontend" / "public-room" / "app.js").read_text("utf-8")
        for secret_name in ("api_key", "API_KEY", "MEMORY_SERVER", "system_prompt"):
            assert secret_name not in frontend, f"frontend exposes forbidden token: {secret_name}"
        page_html = (ROOT / "frontend" / "public-room" / "index.html").read_text("utf-8")
        assert "pixi-live2d-display-cubism4.min.js" in page_html
        assert "runtime/live2d.min.js" not in page_html

        from fastapi.testclient import TestClient

        with TestClient(app, base_url="https://neko.example.test") as client:
            page = client.get("/")
            assert page.status_code == 200
            assert "default-src 'self'" in page.headers["content-security-policy"]
            assert "object-src 'none'" in page.headers["content-security-policy"]
            assert page.headers["strict-transport-security"].startswith(
                "max-age=31536000"
            )
            assert page.headers["x-content-type-options"] == "nosniff"
            assert page.headers["x-frame-options"] == "DENY"
            assert page.headers["cross-origin-resource-policy"] == "same-origin"
            assert client.get("/admin").headers["cache-control"] == "no-store"
            display_runtime = client.get(
                "/runtime/pixi-live2d-display-cubism4.min.js"
            )
            assert display_runtime.status_code == 200
            assert "Copyright (c) 2020 Guan" in display_runtime.text[:300]
            assert client.get("/runtime/live2d.min.js").status_code == 404
            display_license = client.get(
                "/runtime/licenses/pixi-live2d-display-MIT.txt"
            )
            assert display_license.status_code == 200
            assert "Permission is hereby granted" in display_license.text
            oversized = client.post(
                "/api/v1/session/guest",
                content=b"x" * 32769,
                headers={"content-type": "application/json"},
            )
            assert oversized.status_code == 413

            session = client.post("/api/v1/session/guest", json={})
            cookie = session.headers["set-cookie"].lower()
            assert "secure" in cookie and "httponly" in cookie
            assert "samesite=lax" in cookie

        from main_logic.room.avatar import PublicAvatar

        configured_data = Path(temporary) / "configured"
        model_root = configured_data / "live2d" / "licensed-model"
        model_root.mkdir(parents=True)
        (model_root / "licensed.moc3").write_bytes(b"MOC3 verification")
        (model_root / "texture.png").write_bytes(b"PNG verification")
        descriptor = {
            "Version": 3,
            "FileReferences": {
                "Moc": "licensed.moc3",
                "Textures": ["texture.png"],
            },
        }
        (model_root / "licensed.model3.json").write_text(
            json.dumps(descriptor), encoding="utf-8"
        )
        os.environ["NEKO_PUBLIC_LIVE2D_MODEL_NAME"] = "licensed-model"
        os.environ["NEKO_PUBLIC_LIVE2D_MODEL_FILE"] = "licensed.model3.json"
        avatar = PublicAvatar(data_dir=configured_data)
        ready = avatar.manifest()
        assert ready["enabled"] is True and ready["status"] == "ready"
        descriptor["FileReferences"]["Moc"] = "../session.secret"
        (model_root / "licensed.model3.json").write_text(
            json.dumps(descriptor), encoding="utf-8"
        )
        invalid = avatar.manifest()
        assert invalid["enabled"] is False and invalid["status"] == "invalid_model"
    print("public boundary verification passed")


if __name__ == "__main__":
    main()

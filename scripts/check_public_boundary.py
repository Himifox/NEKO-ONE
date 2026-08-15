"""Fail when the lean public FastAPI entry accidentally mounts legacy surfaces."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="neko-boundary-") as temporary:
        os.environ["NEKO_PUBLIC_DATA_DIR"] = temporary
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
    print("public boundary verification passed")


if __name__ == "__main__":
    main()

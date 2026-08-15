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
    print("public boundary verification passed")


if __name__ == "__main__":
    main()

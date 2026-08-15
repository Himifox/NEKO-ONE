"""Start the real Memory app in isolation and enforce the local-only boundary."""

from __future__ import annotations

import os
import sys
import tempfile
import logging
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    temporary_parent = ROOT / "var"
    temporary_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="memory-runtime-verify-",
        dir=temporary_parent,
    ) as temporary:
        runtime_root = Path(temporary).resolve()
        os.environ["NEKO_STORAGE_SELECTED_ROOT"] = str(runtime_root)
        os.environ["NEKO_STORAGE_ANCHOR_ROOT"] = str(runtime_root)
        os.environ["NEKO_LANGUAGE"] = "zh"
        os.environ["VECTORS_ENABLED"] = "0"
        os.environ["_NEKO_MAIN_SERVER_INITIALIZED"] = "1"

        from app.memory_server.runtime import app

        paths = {getattr(route, "path", "") for route in app.routes}
        assert "/health" in paths
        assert not any(path.startswith("/internal/storage/") for path in paths)

        with TestClient(app) as client:
            response = client.get("/health")
            assert response.status_code == 200
            payload = response.json()
            assert payload.get("app") == "N.E.K.O"
            assert payload.get("service") == "memory"
            assert payload.get("status") == "ok"

        forbidden_artifacts = (
            runtime_root / "cloudsave",
            runtime_root / ".cloudsave_staging",
            runtime_root / "cloudsave_backups",
            runtime_root / "state" / "cloudsave_local_state.json",
        )
        created = [
            str(path.relative_to(runtime_root))
            for path in forbidden_artifacts
            if path.exists()
        ]
        assert not created, f"legacy cloud artifacts created: {created}"
        logging.shutdown()

    print("memory runtime verification passed: real startup, health, and local-only storage")


if __name__ == "__main__":
    main()

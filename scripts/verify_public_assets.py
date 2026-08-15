"""Verify every browser-distributed visual asset has an explicit audit record."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB_ROOT = ROOT / "static" / "libs"
MANIFEST = LIB_ROOT / "manifest.json"
FORBIDDEN_PUBLIC_SUFFIXES = {
    ".moc",
    ".moc3",
    ".model3.json",
    ".motion3.json",
    ".exp3.json",
    ".wav",
    ".mp3",
    ".ogg",
    ".flac",
    ".ttf",
    ".otf",
    ".woff",
    ".woff2",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_frontend_dom_contract(html_path: str, script_path: str) -> None:
    html = (ROOT / html_path).read_text("utf-8")
    script = (ROOT / script_path).read_text("utf-8")
    ids = set(re.findall(r'\bid=["\']([^"\']+)["\']', html))
    references = set(
        re.findall(r'document\.getElementById\(["\']([^"\']+)["\']\)', script)
    )
    missing = sorted(references - ids)
    assert not missing, f"{script_path} references missing DOM ids: {missing}"


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    entries = manifest["assets"]
    assert isinstance(entries, list) and entries
    declared_files = {"manifest.json"}
    for entry in entries:
        relative = Path(entry["path"])
        assert not relative.is_absolute() and ".." not in relative.parts
        asset = LIB_ROOT / relative
        assert asset.is_file(), entry["path"]
        assert _sha256(asset) == entry["sha256"], entry["path"]
        assert entry["source"].startswith("https://") or entry["source"].startswith(
            "legacy project import"
        )
        declared_files.add(relative.as_posix())
        license_path = entry.get("license_path")
        if entry["license"] == "MIT":
            assert isinstance(license_path, str)
            license_file = LIB_ROOT / license_path
            assert license_file.is_file()
            assert "Permission is hereby granted" in license_file.read_text("utf-8")
            declared_files.add(Path(license_path).as_posix())
        else:
            assert entry["license_url"].startswith("https://www.live2d.com/eula/")
            header = asset.read_text("utf-8")[:500]
            assert "Redistributable Code" in header
            assert entry["license_url"] in header

    actual_files = {
        path.relative_to(LIB_ROOT).as_posix()
        for path in LIB_ROOT.rglob("*")
        if path.is_file()
    }
    assert actual_files == declared_files, sorted(actual_files ^ declared_files)

    display_bundle = (LIB_ROOT / "pixi-live2d-display-cubism4.min.js").read_text("utf-8")
    assert "PIXI.live2d" in display_bundle
    assert "process.env.NODE_ENV" not in display_bundle
    assert not (LIB_ROOT / "live2d.min.js").exists()
    assert not (ROOT / "assets" / "yui-lolita.tar.gz").exists()

    public_roots = (ROOT / "assets", ROOT / "frontend", ROOT / "static")
    forbidden = []
    for public_root in public_roots:
        if not public_root.exists():
            continue
        for path in public_root.rglob("*"):
            lowered = path.name.lower()
            if path.is_file() and any(
                lowered.endswith(suffix) for suffix in FORBIDDEN_PUBLIC_SUFFIXES
            ):
                forbidden.append(path.relative_to(ROOT).as_posix())
    assert not forbidden, f"unaudited model, voice, or font assets: {forbidden}"

    page = (ROOT / "frontend" / "public-room" / "index.html").read_text("utf-8")
    ordered = (
        "/runtime/live2dcubismcore.min.js",
        "/runtime/pixi.min.js",
        "/runtime/pixi-live2d-display-cubism4.min.js",
        "/assets/live2d.js",
    )
    positions = [page.index(token) for token in ordered]
    assert positions == sorted(positions)
    _assert_frontend_dom_contract(
        "frontend/public-room/index.html", "frontend/public-room/app.js"
    )
    _assert_frontend_dom_contract(
        "frontend/public-admin/index.html", "frontend/public-admin/app.js"
    )
    admin_page = (ROOT / "frontend" / "public-admin" / "index.html").read_text(
        "utf-8"
    )
    assert '<button type="submit">登录</button>' in admin_page
    public_script = (ROOT / "frontend" / "public-room" / "app.js").read_text(
        "utf-8"
    )
    for token in (
        'url.origin !== location.origin',
        'url.pathname.startsWith("/speech-assets/")',
        'window.addEventListener("pagehide"',
        'window.addEventListener("pageshow"',
        "state.socket !== socket",
        'case "room.snapshot"',
        "state.rendered.delete(payload.message_id)",
        "event.room_seq !== state.lastSeq + 1",
        'socket.close(1012, "sequence_gap")',
        'localStorage.setItem("neko.room.soundMuted", "1")',
    ):
        assert token in public_script, f"missing public client lifecycle guard: {token}"
    live2d_script = (ROOT / "frontend" / "public-room" / "live2d.js").read_text(
        "utf-8"
    )
    assert "if (event.persisted) return;" in live2d_script
    environment = (ROOT / ".env.public.example").read_text("utf-8")
    assert "NEKO_PUBLIC_LIVE2D_MODEL_NAME=\n" in environment
    assert "NEKO_PUBLIC_LIVE2D_MODEL_FILE=\n" in environment
    notice = (ROOT / "NOTICE").read_text("utf-8")
    for token in (
        "PixiJS 7.4.3",
        "pixi-live2d-display 0.5.0-beta",
        "Live2D Cubism Core",
    ):
        assert token in notice
    print(
        "public asset verification passed: 3 audited runtimes, "
        "no bundled model, voice, or font; frontend DOM contracts valid"
    )


if __name__ == "__main__":
    main()

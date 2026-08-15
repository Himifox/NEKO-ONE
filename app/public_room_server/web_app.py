"""Allowlisted FastAPI application for the public NEKO room."""

from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from main_logic.room.avatar import PublicAvatar
from main_logic.room.admin_auth import AdminSessionManager
from main_logic.room.service import PublicRoomService
from main_logic.room.session import GuestSessionManager
from main_routers.public_room_router import router as public_router
from main_routers.public_admin_router import router as admin_router
from main_routers.room_websocket_router import router as websocket_router


REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_ROOT = REPO_ROOT / "frontend" / "public-room"
ADMIN_FRONTEND_ROOT = REPO_ROOT / "frontend" / "public-admin"
RUNTIME_ROOT = REPO_ROOT / "static" / "libs"


class AllowlistedStaticFiles(StaticFiles):
    def __init__(self, *, directory: Path, allowed_paths: set[str]):
        super().__init__(directory=directory)
        self.allowed_paths = frozenset(allowed_paths)

    async def get_response(self, path: str, scope: Scope) -> Response:
        normalized = PurePosixPath(path).as_posix()
        if normalized not in self.allowed_paths:
            return Response(status_code=404)
        return await super().get_response(path, scope)


class SpeechStaticFiles(StaticFiles):
    _public_name = re.compile(r"^speech_[0-9a-f]{32}\.wav$")

    async def get_response(self, path: str, scope: Scope) -> Response:
        normalized = PurePosixPath(path).as_posix()
        if "/" in normalized or self._public_name.fullmatch(normalized) is None:
            return Response(status_code=404)
        return await super().get_response(path, scope)

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "base-uri 'none'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        "font-src 'self'",
        "connect-src 'self' ws: wss:",
        "worker-src 'self' blob:",
        "manifest-src 'self'",
    )
)


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _data_dir() -> Path:
    configured = os.environ.get("NEKO_PUBLIC_DATA_DIR", "").strip()
    return Path(configured).expanduser().resolve() if configured else REPO_ROOT / "var" / "public-room"


def create_app() -> FastAPI:
    data_dir = _data_dir()
    database_url = os.environ.get("NEKO_PUBLIC_DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("NEKO_PUBLIC_DATABASE_URL is required")
    service = PublicRoomService(database_url=database_url, data_dir=data_dir)
    sessions = GuestSessionManager(store=service.store, data_dir=data_dir)
    admin_sessions = AdminSessionManager(data_dir)
    avatar = PublicAvatar(data_dir=data_dir)
    avatar.prepare()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        await service.start()
        yield
        await service.shutdown()

    application = FastAPI(
        title="NEKO Public Room",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.room_service = service
    application.state.guest_sessions = sessions
    application.state.admin_sessions = admin_sessions
    application.state.public_avatar = avatar

    max_http_body_bytes = _bounded_env_int(
        "NEKO_PUBLIC_MAX_HTTP_BODY_BYTES", 32768, 1024, 1048576
    )

    @application.middleware("http")
    async def public_security_boundary(request: Request, call_next) -> Response:
        early_status: int | None = None
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError:
                declared_length = -1
            if declared_length < 0:
                early_status = 400
            elif declared_length > max_http_body_bytes:
                early_status = 413

        response = (
            Response(status_code=early_status)
            if early_status is not None
            else await call_next(request)
        )
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        if request.url.path == "/admin" or request.url.path.startswith(
            "/api/v1/admin/"
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    application.include_router(public_router)
    application.include_router(admin_router)
    application.include_router(websocket_router)
    application.mount("/assets", StaticFiles(directory=FRONTEND_ROOT), name="public-assets")
    application.mount(
        "/live2d-assets",
        AllowlistedStaticFiles(
            directory=avatar.assets_root,
            allowed_paths=avatar.public_asset_paths(),
        ),
        name="public-live2d-assets",
    )
    application.mount(
        "/speech-assets",
        SpeechStaticFiles(directory=service.speech.audio_root),
        name="public-speech-assets",
    )
    application.mount(
        "/runtime",
        StaticFiles(directory=RUNTIME_ROOT),
        name="audited-browser-runtime",
    )

    @application.get("/", include_in_schema=False)
    async def public_room_page() -> FileResponse:
        return FileResponse(FRONTEND_ROOT / "index.html")

    @application.get("/admin", include_in_schema=False)
    async def admin_page() -> FileResponse:
        return FileResponse(ADMIN_FRONTEND_ROOT / "index.html")

    application.mount(
        "/admin-assets",
        StaticFiles(directory=ADMIN_FRONTEND_ROOT),
        name="public-admin-assets",
    )

    return application


app = create_app()

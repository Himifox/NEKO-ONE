"""Allowlisted FastAPI application for the public NEKO room."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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


def _data_dir() -> Path:
    configured = os.environ.get("NEKO_PUBLIC_DATA_DIR", "").strip()
    return Path(configured).expanduser().resolve() if configured else REPO_ROOT / "var" / "public-room"


def create_app() -> FastAPI:
    data_dir = _data_dir()
    service = PublicRoomService(database_path=data_dir / "public-room.db")
    sessions = GuestSessionManager(store=service.store, data_dir=data_dir)
    admin_sessions = AdminSessionManager(data_dir)
    avatar = PublicAvatar(repo_root=REPO_ROOT, data_dir=data_dir)
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
    application.include_router(public_router)
    application.include_router(admin_router)
    application.include_router(websocket_router)
    application.mount("/assets", StaticFiles(directory=FRONTEND_ROOT), name="public-assets")
    application.mount(
        "/live2d-assets",
        StaticFiles(directory=avatar.assets_root),
        name="public-live2d-assets",
    )
    application.mount(
        "/speech-assets",
        StaticFiles(directory=service.speech.audio_root),
        name="public-speech-assets",
    )

    @application.get("/runtime/pixi.min.js", include_in_schema=False)
    async def pixi_runtime() -> FileResponse:
        return FileResponse(REPO_ROOT / "static" / "libs" / "pixi.min.js")

    @application.get("/runtime/live2dcubismcore.min.js", include_in_schema=False)
    async def cubism_runtime() -> FileResponse:
        return FileResponse(REPO_ROOT / "static" / "libs" / "live2dcubismcore.min.js")

    @application.get("/runtime/live2d.min.js", include_in_schema=False)
    async def live2d_runtime() -> FileResponse:
        return FileResponse(REPO_ROOT / "static" / "libs" / "live2d.min.js")

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

"""Opaque signed guest sessions for the public room."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

from .models import Visitor
from .store import RoomStore


COOKIE_NAME = "neko_public_session"


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class GuestSessionManager:
    def __init__(self, *, store: RoomStore, data_dir: Path):
        self.store = store
        self.data_dir = Path(data_dir)
        self._secret = self._load_or_create_secret()
        self.max_age_seconds = int(
            os.environ.get("NEKO_PUBLIC_SESSION_MAX_AGE", str(60 * 60 * 24 * 90))
        )

    def _load_or_create_secret(self) -> bytes:
        configured = os.environ.get("NEKO_PUBLIC_SESSION_SECRET", "").strip()
        if configured:
            return configured.encode("utf-8")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        secret_path = self.data_dir / "session.secret"
        try:
            value = secret_path.read_bytes().strip()
            if len(value) >= 32:
                return value
        except FileNotFoundError:
            pass
        value = secrets.token_urlsafe(48).encode("ascii")
        temp_path = secret_path.with_suffix(".tmp")
        temp_path.write_bytes(value)
        os.replace(temp_path, secret_path)
        return value

    def issue(self, visitor: Visitor) -> str:
        issued_at = int(time.time())
        body = _b64encode(f"{visitor.id}|{issued_at}".encode("utf-8"))
        signature = _b64encode(
            hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest()
        )
        return f"{body}.{signature}"

    async def resolve(self, token: str | None) -> Visitor | None:
        visitor_id = self._verify(token)
        if not visitor_id:
            return None
        visitor = await self.store.get_visitor(visitor_id)
        if visitor is None or visitor.status != "active":
            return None
        await self.store.touch_visitor(visitor.id)
        return visitor

    def _verify(self, token: str | None) -> str | None:
        if not token or "." not in token:
            return None
        body, signature = token.split(".", 1)
        expected = _b64encode(
            hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            visitor_id, issued_text = _b64decode(body).decode("utf-8").split("|", 1)
            issued_at = int(issued_text)
        except (ValueError, UnicodeDecodeError):
            return None
        now = int(time.time())
        if issued_at > now + 60 or now - issued_at > self.max_age_seconds:
            return None
        if not visitor_id.startswith("vis_") or len(visitor_id) > 80:
            return None
        return visitor_id

"""Password bootstrap and short-lived signed admin sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path


ADMIN_COOKIE_NAME = "neko_public_admin"


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class AdminSessionManager:
    def __init__(self, data_dir: Path):
        self.password = os.environ.get("NEKO_PUBLIC_ADMIN_PASSWORD", "")
        self.max_age_seconds = 12 * 60 * 60
        self._secret = self._load_secret(data_dir)
        self._attempts: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=12))

    @property
    def enabled(self) -> bool:
        return len(self.password) >= 12

    @staticmethod
    def _load_secret(data_dir: Path) -> bytes:
        path = data_dir / "admin-session.secret"
        try:
            existing = path.read_bytes().strip()
            if len(existing) >= 32:
                return existing
        except FileNotFoundError:
            pass
        value = secrets.token_bytes(48)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(value)
        os.replace(temporary, path)
        return value

    def authenticate(self, password: str, remote: str) -> tuple[str, str] | None:
        if not self.enabled:
            return None
        now = time.monotonic()
        attempts = self._attempts[remote]
        while attempts and now - attempts[0] > 60:
            attempts.popleft()
        if len(attempts) >= 5:
            return None
        if not hmac.compare_digest(str(password), self.password):
            attempts.append(now)
            return None
        attempts.clear()
        csrf = secrets.token_urlsafe(24)
        issued = int(time.time())
        body = _encode(f"{issued}|{csrf}".encode("utf-8"))
        signature = _encode(hmac.new(self._secret, body.encode(), hashlib.sha256).digest())
        return f"{body}.{signature}", csrf

    def resolve(self, token: str | None) -> str | None:
        if not self.enabled or not token or "." not in token:
            return None
        body, signature = token.split(".", 1)
        expected = _encode(hmac.new(self._secret, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            issued_text, csrf = _decode(body).decode("utf-8").split("|", 1)
            issued = int(issued_text)
        except (ValueError, UnicodeDecodeError):
            return None
        age = int(time.time()) - issued
        if age < -60 or age > self.max_age_seconds or len(csrf) < 20:
            return None
        return csrf

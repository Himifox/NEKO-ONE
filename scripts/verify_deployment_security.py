"""Verify that the production examples preserve the public trust boundary."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text("utf-8")


def main() -> None:
    nginx = _read("deploy/nginx-neko-public.conf")
    required_nginx = {
        "TLS 1.2/1.3": "ssl_protocols TLSv1.2 TLSv1.3;",
        "HSTS": "add_header Strict-Transport-Security",
        "CSP": "add_header Content-Security-Policy",
        "frame isolation": "frame-ancestors 'none'",
        "MIME sniffing protection": 'add_header X-Content-Type-Options "nosniff" always;',
        "permissions policy": "add_header Permissions-Policy",
        "HTTP body limit": "client_max_body_size 32k;",
        "HTTP connection limit": "limit_conn neko_client 20;",
        "WebSocket connection limit": "limit_conn neko_ws 3;",
        "guest session rate limit": "limit_req zone=neko_session",
        "admin rate limit": "limit_req zone=neko_admin",
        "general API rate limit": "limit_req zone=neko_http",
        "429 limit response": "limit_req_status 429;",
        "WebSocket upgrade map": "map $http_upgrade $neko_connection_upgrade",
        "private readiness probe": "location = /api/v1/health/ready",
        "loopback readiness allowlist": "allow 127.0.0.1;",
        "production hostname": "server_name neko.pardofelis.wiki;",
        "production certificate": "/etc/letsencrypt/live/neko.pardofelis.wiki/fullchain.pem",
    }
    missing = [label for label, token in required_nginx.items() if token not in nginx]
    assert not missing, f"missing Nginx controls: {missing}"

    upstreams = re.findall(r"proxy_pass\s+([^;]+);", nginx)
    assert upstreams, "Nginx example has no upstream"
    assert set(upstreams) == {
        "http://127.0.0.1:48911"
    }, f"non-loopback public upstream found: {upstreams}"
    assert "listen 443 ssl" in nginx
    assert "ssl_reject_handshake on;" in nginx
    assert not re.search(r"listen\s+[^;]*\b0\.0\.0\.0\b", nginx)

    environment = _read(".env.public.example")
    for token in (
        "NEKO_PUBLIC_HOST=127.0.0.1",
        "NEKO_PUBLIC_FORWARDED_ALLOW_IPS=127.0.0.1",
        "NEKO_PUBLIC_ALLOW_MISSING_ORIGIN=0",
        "NEKO_PUBLIC_ALLOWED_ORIGINS=https://neko.pardofelis.wiki",
        "NEKO_PUBLIC_MAX_HTTP_BODY_BYTES=32768",
        "NEKO_PUBLIC_WS_MAX_SIZE_BYTES=16384",
        "NEKO_PUBLIC_WS_MAX_FRAME_CHARS=8192",
        "NEKO_PUBLIC_MIN_FREE_MIB=256",
        "NEKO_PUBLIC_LIVE2D_MODEL_NAME=",
        "NEKO_PUBLIC_LIVE2D_MODEL_FILE=",
    ):
        assert token in environment, f"missing public environment boundary: {token}"

    entrypoint = _read("app/public_room_server/__main__.py")
    for token in ("ws_max_size=", "ws_max_queue=", "timeout_keep_alive="):
        assert token in entrypoint, f"Uvicorn limit is not wired: {token}"

    unit = _read("deploy/neko-public.service")
    for token in (
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "PrivateTmp=true",
        "CapabilityBoundingSet=",
    ):
        assert token in unit, f"missing systemd sandbox control: {token}"
    assert "Requires=neko-memory.service" not in unit, (
        "Memory must remain a weak dependency so its outage cannot stop the room"
    )

    print("deployment security verification passed")


if __name__ == "__main__":
    main()

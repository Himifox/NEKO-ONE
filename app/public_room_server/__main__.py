"""Run the public-room server directly with ``python -m``."""

from __future__ import annotations

import os

import uvicorn


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def main() -> None:
    uvicorn.run(
        "app.public_room_server.web_app:app",
        host=os.environ.get("NEKO_PUBLIC_HOST", "127.0.0.1"),
        port=int(os.environ.get("NEKO_PUBLIC_PORT", "48911")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("NEKO_PUBLIC_FORWARDED_ALLOW_IPS", "127.0.0.1"),
        ws_max_size=_bounded_env_int(
            "NEKO_PUBLIC_WS_MAX_SIZE_BYTES", 16384, 4096, 1048576
        ),
        ws_max_queue=_bounded_env_int("NEKO_PUBLIC_WS_MAX_QUEUE", 32, 1, 128),
        timeout_keep_alive=_bounded_env_int(
            "NEKO_PUBLIC_HTTP_KEEP_ALIVE_SECONDS", 5, 1, 30
        ),
    )


if __name__ == "__main__":
    main()

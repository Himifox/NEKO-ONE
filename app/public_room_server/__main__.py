"""Run the public-room server directly with ``python -m``."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "app.public_room_server.web_app:app",
        host=os.environ.get("NEKO_PUBLIC_HOST", "127.0.0.1"),
        port=int(os.environ.get("NEKO_PUBLIC_PORT", "48911")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("NEKO_PUBLIC_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()

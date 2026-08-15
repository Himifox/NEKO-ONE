"""Minimal local persistence guard for the single-instance public runtime."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Iterator


class LocalWriteUnavailable(RuntimeError):
    """Stable error shape for a temporarily unavailable local write."""

    code = "local_write_unavailable"

    def __init__(self, *, operation: str = "write", target: str = "") -> None:
        super().__init__(f"local write unavailable: {operation}")
        self.mode = "local"
        self.operation = operation
        self.target = target


def assert_local_writable(
    _config_manager: Any,
    *,
    operation: str = "write",
    target: str = "",
) -> None:
    """Compatibility hook; real filesystem writes remain the source of truth.

    NEKO-ONE has one local writer and no cloud-import process, so there is no
    second writer to fence. Atomic persistence functions still surface
    permission, read-only filesystem, and disk-full errors directly.
    """


def local_write_error_payload(exc: LocalWriteUnavailable) -> dict[str, Any]:
    return {
        "success": False,
        "error": exc.code,
        "code": exc.code,
        "mode": exc.mode,
        "operation": exc.operation,
        "target": exc.target,
        "retryable": True,
    }


@contextmanager
def local_writable_transaction(
    config_manager: Any,
    *,
    operation: str = "write",
    target: str = "",
) -> Iterator[None]:
    assert_local_writable(config_manager, operation=operation, target=target)
    yield


@asynccontextmanager
async def async_local_writable_transaction(
    config_manager: Any,
    *,
    operation: str = "write",
    target: str = "",
) -> AsyncIterator[None]:
    assert_local_writable(config_manager, operation=operation, target=target)
    yield

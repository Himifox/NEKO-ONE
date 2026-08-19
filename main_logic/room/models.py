"""Small transport-neutral models for the public-room runtime."""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class CursorState:
    """Aggregated per-zone cursor attention for a room."""

    zone: str
    visitor_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class Visitor:
    id: str
    display_name: str
    status: str = "active"


@dataclass(slots=True)
class RoomMessage:
    id: str
    room_id: str
    room_seq: int
    author_type: str
    author_id: str
    display_name: str
    content: str
    created_at: str
    reply_to_id: str | None = None
    status: str = "visible"
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_public_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "room_id": self.room_id,
            "room_seq": self.room_seq,
            "author_type": self.author_type,
            "display_name": self.display_name,
            "content": self.content,
            "created_at": self.created_at,
            "reply_to_id": self.reply_to_id,
            "status": self.status,
        }


@dataclass(slots=True)
class TurnCandidate:
    message: RoomMessage
    enqueued_monotonic: float
    mentioned_neko: bool = False


@dataclass(slots=True)
class ActiveGeneration:
    id: str
    room_id: str
    target_visitor_id: str
    source_message_ids: list[str]
    text: str = ""
    chunk_index: int = 0
    phase: str = "preparing"
    cancel_reason: str | None = None
    started_at: str = field(default_factory=utc_now)

    def snapshot(self) -> dict[str, Any]:
        return {
            "generation_id": self.id,
            "room_id": self.room_id,
            "target_visitor_id": self.target_visitor_id,
            "source_message_ids": list(self.source_message_ids),
            "text": self.text,
            "chunk_index": self.chunk_index,
            "phase": self.phase,
            "cancellable": self.phase in {"preparing", "generating"},
            "started_at": self.started_at,
        }

    def public_snapshot(self) -> dict[str, Any]:
        return {
            "generation_id": self.id,
            "room_id": self.room_id,
            "text": self.text,
            "chunk_index": self.chunk_index,
            "phase": self.phase,
            "cancellable": self.phase in {"preparing", "generating"},
            "started_at": self.started_at,
        }

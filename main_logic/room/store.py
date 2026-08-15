"""PostgreSQL persistence for ordered public-room events.

The first release has one process-wide writer. A process-local asyncio lock and
a PostgreSQL row lock make sequence allocation, message insertion, event
insertion, and request idempotency one transaction.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from .models import RoomMessage, Visitor, utc_now


SCHEMA_VERSION = 1


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS visitors (
        id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rooms (
        id TEXT PRIMARY KEY,
        slug TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'active',
        last_seq BIGINT NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        room_id TEXT NOT NULL REFERENCES rooms(id),
        room_seq BIGINT NOT NULL CHECK (room_seq > 0),
        author_type TEXT NOT NULL,
        author_id TEXT NOT NULL,
        display_name TEXT NOT NULL,
        reply_to_id TEXT,
        content TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'visible',
        metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TEXT NOT NULL,
        UNIQUE(room_id, room_seq)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_room_seq ON messages(room_id, room_seq)",
    """
    CREATE TABLE IF NOT EXISTS room_events (
        id TEXT PRIMARY KEY,
        room_id TEXT NOT NULL REFERENCES rooms(id),
        room_seq BIGINT NOT NULL CHECK (room_seq > 0),
        type TEXT NOT NULL,
        payload_json JSONB NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(room_id, room_seq)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_room_events_room_seq ON room_events(room_id, room_seq)",
    """
    CREATE TABLE IF NOT EXISTS client_requests (
        visitor_id TEXT NOT NULL,
        request_id TEXT NOT NULL,
        result_json JSONB NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(visitor_id, request_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS turns (
        id TEXT PRIMARY KEY,
        room_id TEXT NOT NULL REFERENCES rooms(id),
        target_visitor_id TEXT,
        source_message_ids_json JSONB NOT NULL,
        reason_code TEXT NOT NULL,
        decision_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        error_code TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id TEXT PRIMARY KEY,
        actor_id TEXT NOT NULL,
        action TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS service_settings (
        key TEXT PRIMARY KEY,
        value_json JSONB NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)


def _decode_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


class _Connection:
    """Small DB-API compatibility wrapper while the store keeps positional SQL."""

    def __init__(self, connection: psycopg.Connection):
        self._connection = connection

    def execute(self, query: str, params: Any = None):
        statement = query.replace("?", "%s")
        return self._connection.execute(statement, params)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


class RoomStore:
    def __init__(self, database_url: str, *, data_dir: Path):
        normalized = str(database_url or "").strip()
        if not normalized.startswith(("postgresql://", "postgres://")):
            raise ValueError("NEKO_PUBLIC_DATABASE_URL must be a PostgreSQL URL")
        self.database_url = normalized
        self.data_dir = Path(data_dir)
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize_sync)

    def _connect(self, *, timeout_seconds: int = 5) -> _Connection:
        connection = psycopg.connect(
            self.database_url,
            connect_timeout=max(1, int(timeout_seconds)),
            row_factory=dict_row,
            application_name="neko-one-public",
            options=(
                "-c statement_timeout=10000 "
                "-c lock_timeout=5000 "
                "-c idle_in_transaction_session_timeout=10000"
            ),
        )
        return _Connection(connection)

    @contextmanager
    def _managed_connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_sync(self) -> None:
        with self._managed_connection() as connection:
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            now = utc_now()
            connection.execute(
                """
                INSERT INTO rooms(id, slug, status, last_seq, created_at, updated_at)
                VALUES('main', 'main', 'active', 0, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (now, now),
            )
            connection.execute(
                """
                INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)
                ON CONFLICT(version) DO NOTHING
                """,
                (SCHEMA_VERSION, now),
            )
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
            if version_row is None or int(version_row["version"]) != SCHEMA_VERSION:
                raise RuntimeError("unsupported PostgreSQL schema version")

    async def readiness_snapshot(self, *, minimum_free_mib: int) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._readiness_snapshot_sync, max(1, int(minimum_free_mib))
        )

    def _readiness_snapshot_sync(self, minimum_free_mib: int) -> dict[str, Any]:
        """Prove integrity, a real rollbackable write, and minimum disk headroom."""

        free_bytes = shutil.disk_usage(self.data_dir).free
        free_mib = free_bytes // (1024 * 1024)
        result: dict[str, Any] = {
            "ok": False,
            "schema_version": None,
            "writable": False,
            "disk_space_ok": free_mib >= minimum_free_mib,
            "error_code": None,
        }
        connection: _Connection | None = None
        try:
            connection = self._connect(timeout_seconds=2)
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
            version = int(version_row["version"]) if version_row else 0
            result["schema_version"] = version
            if version != SCHEMA_VERSION:
                result["error_code"] = "schema_mismatch"
                return result
            connection.execute(
                """
                INSERT INTO service_settings(key, value_json, updated_at)
                VALUES('__readiness_probe__', '{}', ?)
                ON CONFLICT(key) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (utc_now(),),
            )
            connection.rollback()
            result["writable"] = True
            if free_mib < minimum_free_mib:
                result["error_code"] = "disk_space"
                return result
            result["ok"] = True
            return result
        except psycopg.Error:
            result["error_code"] = "postgres_unavailable"
            return result
        except OSError:
            result["error_code"] = "storage_unavailable"
            return result
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                except psycopg.Error:
                    pass
                connection.close()

    async def create_visitor(self, display_name: str) -> Visitor:
        visitor = Visitor(id=f"vis_{uuid4().hex}", display_name=display_name)
        now = utc_now()
        async with self._write_lock:
            await asyncio.to_thread(self._create_visitor_sync, visitor, now)
        return visitor

    def _create_visitor_sync(self, visitor: Visitor, now: str) -> None:
        with self._managed_connection() as connection:
            connection.execute(
                "INSERT INTO visitors(id, display_name, status, created_at, last_seen_at) VALUES(?, ?, ?, ?, ?)",
                (visitor.id, visitor.display_name, visitor.status, now, now),
            )

    async def get_visitor(self, visitor_id: str) -> Visitor | None:
        return await asyncio.to_thread(self._get_visitor_sync, visitor_id)

    def _get_visitor_sync(self, visitor_id: str) -> Visitor | None:
        with self._managed_connection() as connection:
            row = connection.execute(
                "SELECT id, display_name, status FROM visitors WHERE id = ?",
                (visitor_id,),
            ).fetchone()
        if row is None:
            return None
        return Visitor(id=row["id"], display_name=row["display_name"], status=row["status"])

    async def touch_visitor(self, visitor_id: str) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._touch_visitor_sync, visitor_id)

    def _touch_visitor_sync(self, visitor_id: str) -> None:
        with self._managed_connection() as connection:
            connection.execute(
                "UPDATE visitors SET last_seen_at = ? WHERE id = ?",
                (utc_now(), visitor_id),
            )

    async def append_user_message(
        self,
        *,
        room_id: str,
        visitor: Visitor,
        request_id: str,
        content: str,
        reply_to_id: str | None = None,
    ) -> tuple[RoomMessage, dict[str, Any], bool]:
        async with self._write_lock:
            return await asyncio.to_thread(
                self._append_user_message_sync,
                room_id,
                visitor,
                request_id,
                content,
                reply_to_id,
            )

    async def find_client_request(
        self, visitor_id: str, request_id: str
    ) -> tuple[RoomMessage, dict[str, Any]] | None:
        return await asyncio.to_thread(
            self._find_client_request_sync, visitor_id, request_id
        )

    def _find_client_request_sync(
        self, visitor_id: str, request_id: str
    ) -> tuple[RoomMessage, dict[str, Any]] | None:
        with self._managed_connection() as connection:
            row = connection.execute(
                "SELECT result_json FROM client_requests WHERE visitor_id = ? AND request_id = ?",
                (visitor_id, request_id),
            ).fetchone()
            if row is None:
                return None
            result = _decode_json(row["result_json"], {})
            message = self._message_by_id_on(connection, result["message_id"])
            return message, result["event"]

    def _append_user_message_sync(
        self,
        room_id: str,
        visitor: Visitor,
        request_id: str,
        content: str,
        reply_to_id: str | None,
    ) -> tuple[RoomMessage, dict[str, Any], bool]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            previous = connection.execute(
                "SELECT result_json FROM client_requests WHERE visitor_id = ? AND request_id = ?",
                (visitor.id, request_id),
            ).fetchone()
            if previous is not None:
                result = _decode_json(previous["result_json"], {})
                message = self._message_by_id_on(connection, result["message_id"])
                connection.commit()
                return message, result["event"], True

            message, event = self._append_message_on(
                connection,
                room_id=room_id,
                author_type="visitor",
                author_id=visitor.id,
                display_name=visitor.display_name,
                content=content,
                reply_to_id=reply_to_id,
                metadata={},
            )
            result = {"message_id": message.id, "event": event}
            connection.execute(
                "INSERT INTO client_requests(visitor_id, request_id, result_json, created_at) VALUES(?, ?, ?::jsonb, ?)",
                (visitor.id, request_id, json.dumps(result, ensure_ascii=False), utc_now()),
            )
            connection.commit()
            return message, event, False
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def append_assistant_message(
        self,
        *,
        room_id: str,
        character_id: str,
        display_name: str,
        content: str,
        reply_to_id: str | None,
        metadata: dict[str, Any],
    ) -> tuple[RoomMessage, dict[str, Any]]:
        async with self._write_lock:
            return await asyncio.to_thread(
                self._append_assistant_message_sync,
                room_id,
                character_id,
                display_name,
                content,
                reply_to_id,
                metadata,
            )

    def _append_assistant_message_sync(
        self,
        room_id: str,
        character_id: str,
        display_name: str,
        content: str,
        reply_to_id: str | None,
        metadata: dict[str, Any],
    ) -> tuple[RoomMessage, dict[str, Any]]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            message, event = self._append_message_on(
                connection,
                room_id=room_id,
                author_type="neko",
                author_id=character_id,
                display_name=display_name,
                content=content,
                reply_to_id=reply_to_id,
                metadata=metadata,
            )
            connection.commit()
            return message, event
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _append_message_on(
        self,
        connection: _Connection,
        *,
        room_id: str,
        author_type: str,
        author_id: str,
        display_name: str,
        content: str,
        reply_to_id: str | None,
        metadata: dict[str, Any],
    ) -> tuple[RoomMessage, dict[str, Any]]:
        seq = self._next_seq_on(connection, room_id)
        message = RoomMessage(
            id=f"msg_{uuid4().hex}",
            room_id=room_id,
            room_seq=seq,
            author_type=author_type,
            author_id=author_id,
            display_name=display_name,
            content=content,
            reply_to_id=reply_to_id,
            created_at=utc_now(),
            metadata=metadata,
        )
        connection.execute(
            """
            INSERT INTO messages(
                id, room_id, room_seq, author_type, author_id, display_name,
                reply_to_id, content, status, metadata_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?)
            """,
            (
                message.id,
                message.room_id,
                message.room_seq,
                message.author_type,
                message.author_id,
                message.display_name,
                message.reply_to_id,
                message.content,
                message.status,
                json.dumps(message.metadata, ensure_ascii=False),
                message.created_at,
            ),
        )
        event = self._insert_event_on(
            connection,
            room_id=room_id,
            room_seq=seq,
            event_type="message.created",
            payload=message.as_public_payload(),
        )
        return message, event

    async def append_event(
        self, room_id: str, event_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._write_lock:
            return await asyncio.to_thread(
                self._append_event_sync, room_id, event_type, payload
            )

    def _append_event_sync(
        self, room_id: str, event_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            seq = self._next_seq_on(connection, room_id)
            event = self._insert_event_on(
                connection,
                room_id=room_id,
                room_seq=seq,
                event_type=event_type,
                payload=payload,
            )
            connection.commit()
            return event
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _next_seq_on(self, connection: _Connection, room_id: str) -> int:
        now = utc_now()
        connection.execute(
            """
            INSERT INTO rooms(id, slug, status, last_seq, created_at, updated_at)
            VALUES(?, ?, 'active', 0, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (room_id, room_id, now, now),
        )
        row = connection.execute(
            "SELECT last_seq FROM rooms WHERE id = ? FOR UPDATE", (room_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("room row disappeared during sequence allocation")
        current = int(row["last_seq"])
        next_seq = current + 1
        connection.execute(
            "UPDATE rooms SET last_seq = ?, updated_at = ? WHERE id = ?",
            (next_seq, utc_now(), room_id),
        )
        return next_seq

    def _insert_event_on(
        self,
        connection: _Connection,
        *,
        room_id: str,
        room_seq: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        event = {
            "type": event_type,
            "event_id": f"evt_{uuid4().hex}",
            "room_seq": room_seq,
            "server_time": utc_now(),
            "payload": payload,
        }
        connection.execute(
            "INSERT INTO room_events(id, room_id, room_seq, type, payload_json, created_at) VALUES(?, ?, ?, ?, ?::jsonb, ?)",
            (
                event["event_id"],
                room_id,
                room_seq,
                event_type,
                json.dumps(payload, ensure_ascii=False),
                event["server_time"],
            ),
        )
        return event

    def _message_by_id_on(
        self, connection: _Connection, message_id: str
    ) -> RoomMessage:
        row = connection.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("idempotency record points to a missing message")
        return self._row_to_message(row)

    async def list_events(
        self, room_id: str, after_seq: int, limit: int = 500
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_events_sync, room_id, after_seq, limit)

    def _list_events_sync(
        self, room_id: str, after_seq: int, limit: int
    ) -> list[dict[str, Any]]:
        with self._managed_connection() as connection:
            rows = connection.execute(
                """
                SELECT id, room_seq, type, payload_json, created_at
                FROM room_events WHERE room_id = ? AND room_seq > ?
                ORDER BY room_seq ASC LIMIT ?
                """,
                (room_id, max(0, after_seq), max(1, min(limit, 2000))),
            ).fetchall()
        return [
            {
                "type": row["type"],
                "event_id": row["id"],
                "room_seq": row["room_seq"],
                "server_time": row["created_at"],
                "payload": _decode_json(row["payload_json"], {}),
            }
            for row in rows
        ]

    async def list_messages(
        self, room_id: str, *, before_seq: int | None = None, limit: int = 100
    ) -> list[RoomMessage]:
        return await asyncio.to_thread(
            self._list_messages_sync, room_id, before_seq, limit
        )

    def _list_messages_sync(
        self, room_id: str, before_seq: int | None, limit: int
    ) -> list[RoomMessage]:
        where = "room_id = ? AND status = 'visible'"
        params: list[Any] = [room_id]
        if before_seq is not None:
            where += " AND room_seq < ?"
            params.append(before_seq)
        params.append(max(1, min(limit, 500)))
        with self._managed_connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM messages WHERE {where} ORDER BY room_seq DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_message(row) for row in reversed(rows)]

    async def room_snapshot(self, room_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._room_snapshot_sync, room_id)

    def _room_snapshot_sync(self, room_id: str) -> dict[str, Any]:
        with self._managed_connection() as connection:
            room = connection.execute(
                "SELECT id, slug, status, last_seq FROM rooms WHERE id = ?",
                (room_id,),
            ).fetchone()
            oldest = connection.execute(
                "SELECT MIN(room_seq) AS oldest FROM room_events WHERE room_id = ?",
                (room_id,),
            ).fetchone()["oldest"]
        if room is None:
            return {
                "id": room_id,
                "slug": room_id,
                "status": "missing",
                "last_seq": 0,
                "oldest_available_seq": 1,
            }
        snapshot = dict(room)
        snapshot["oldest_available_seq"] = (
            int(oldest) if oldest is not None else int(room["last_seq"]) + 1
        )
        return snapshot

    async def room_history_snapshot(
        self, room_id: str, *, limit: int = 100
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._room_history_snapshot_sync, room_id, limit
        )

    def _room_history_snapshot_sync(
        self, room_id: str, limit: int
    ) -> dict[str, Any]:
        with self._managed_connection() as connection:
            connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            room = connection.execute(
                "SELECT id, slug, status, last_seq FROM rooms WHERE id = ?",
                (room_id,),
            ).fetchone()
            oldest = connection.execute(
                "SELECT MIN(room_seq) AS oldest FROM room_events WHERE room_id = ?",
                (room_id,),
            ).fetchone()["oldest"]
            rows = connection.execute(
                """
                SELECT * FROM messages
                WHERE room_id = ? AND status = 'visible'
                ORDER BY room_seq DESC LIMIT ?
                """,
                (room_id, max(1, min(limit, 200))),
            ).fetchall()
        if room is None:
            return {
                "id": room_id,
                "slug": room_id,
                "status": "missing",
                "last_seq": 0,
                "oldest_available_seq": 1,
                "messages": [],
            }
        snapshot = dict(room)
        snapshot["oldest_available_seq"] = (
            int(oldest) if oldest is not None else int(room["last_seq"]) + 1
        )
        snapshot["messages"] = [
            self._row_to_message(row).as_public_payload() for row in reversed(rows)
        ]
        return snapshot

    async def list_stale_visitors(
        self, before: str, *, limit: int = 100
    ) -> list[Visitor]:
        return await asyncio.to_thread(
            self._list_stale_visitors_sync, before, limit
        )

    def _list_stale_visitors_sync(
        self, before: str, limit: int
    ) -> list[Visitor]:
        with self._managed_connection() as connection:
            rows = connection.execute(
                """
                SELECT id, display_name, status
                FROM visitors
                WHERE last_seen_at < ?
                ORDER BY last_seen_at ASC
                LIMIT ?
                """,
                (before, max(1, min(limit, 500))),
            ).fetchall()
        return [
            Visitor(
                id=row["id"],
                display_name=row["display_name"],
                status=row["status"],
            )
            for row in rows
        ]

    async def cleanup_expired(
        self,
        *,
        content_before: str,
        audit_before: str,
        visitor_ids: list[str],
        actor_id: str = "system:retention",
    ) -> dict[str, int]:
        async with self._write_lock:
            return await asyncio.to_thread(
                self._cleanup_expired_sync,
                content_before,
                audit_before,
                visitor_ids,
                actor_id,
            )

    def _cleanup_expired_sync(
        self,
        content_before: str,
        audit_before: str,
        visitor_ids: list[str],
        actor_id: str,
    ) -> dict[str, int]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            counts = {
                "client_requests": connection.execute(
                    "DELETE FROM client_requests WHERE created_at < ?",
                    (content_before,),
                ).rowcount,
                "turns": connection.execute(
                    """
                    DELETE FROM turns
                    WHERE status != 'running'
                      AND COALESCE(completed_at, started_at) < ?
                    """,
                    (content_before,),
                ).rowcount,
                "messages": connection.execute(
                    "DELETE FROM messages WHERE created_at < ?",
                    (content_before,),
                ).rowcount,
                "events": connection.execute(
                    "DELETE FROM room_events WHERE created_at < ?",
                    (content_before,),
                ).rowcount,
                "audit": connection.execute(
                    "DELETE FROM audit_log WHERE created_at < ?",
                    (audit_before,),
                ).rowcount,
                "visitors": 0,
            }
            safe_visitor_ids = sorted(
                {
                    visitor_id
                    for visitor_id in visitor_ids
                    if visitor_id.startswith("vis_") and len(visitor_id) <= 80
                }
            )
            if safe_visitor_ids:
                placeholders = ",".join("?" for _ in safe_visitor_ids)
                counts["visitors"] = connection.execute(
                    f"DELETE FROM visitors WHERE id IN ({placeholders})",
                    safe_visitor_ids,
                ).rowcount
            self._insert_audit_on(
                connection,
                actor_id,
                "retention.cleanup",
                "service",
                "public-room",
                {
                    "content_before": content_before,
                    "audit_before": audit_before,
                    "counts": counts,
                },
            )
            connection.commit()
            return {key: int(value) for key, value in counts.items()}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _row_to_message(row: dict[str, Any]) -> RoomMessage:
        return RoomMessage(
            id=row["id"],
            room_id=row["room_id"],
            room_seq=int(row["room_seq"]),
            author_type=row["author_type"],
            author_id=row["author_id"],
            display_name=row["display_name"],
            reply_to_id=row["reply_to_id"],
            content=row["content"],
            status=row["status"],
            metadata=_decode_json(row["metadata_json"], {}),
            created_at=row["created_at"],
        )

    async def start_turn(
        self,
        *,
        turn_id: str,
        room_id: str,
        target_visitor_id: str | None,
        source_message_ids: list[str],
        reason_code: str,
        decision: dict[str, Any],
    ) -> None:
        async with self._write_lock:
            await asyncio.to_thread(
                self._start_turn_sync,
                turn_id,
                room_id,
                target_visitor_id,
                source_message_ids,
                reason_code,
                decision,
            )

    def _start_turn_sync(
        self,
        turn_id: str,
        room_id: str,
        target_visitor_id: str | None,
        source_message_ids: list[str],
        reason_code: str,
        decision: dict[str, Any],
    ) -> None:
        with self._managed_connection() as connection:
            connection.execute(
                """
                INSERT INTO turns(
                    id, room_id, target_visitor_id, source_message_ids_json,
                    reason_code, decision_json, status, started_at
                ) VALUES(?, ?, ?, ?::jsonb, ?, ?::jsonb, 'generating', ?)
                """,
                (
                    turn_id,
                    room_id,
                    target_visitor_id,
                    json.dumps(source_message_ids),
                    reason_code,
                    json.dumps(decision, ensure_ascii=False),
                    utc_now(),
                ),
            )

    async def finish_turn(
        self, turn_id: str, *, status: str, error_code: str | None = None
    ) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._finish_turn_sync, turn_id, status, error_code)

    def _finish_turn_sync(
        self, turn_id: str, status: str, error_code: str | None
    ) -> None:
        with self._managed_connection() as connection:
            connection.execute(
                "UPDATE turns SET status = ?, completed_at = ?, error_code = ? WHERE id = ?",
                (status, utc_now(), error_code, turn_id),
            )

    async def admin_snapshot(self, *, limit: int = 100) -> dict[str, Any]:
        return await asyncio.to_thread(self._admin_snapshot_sync, limit)

    def _admin_snapshot_sync(self, limit: int) -> dict[str, Any]:
        safe_limit = max(1, min(limit, 500))
        with self._managed_connection() as connection:
            visitors = connection.execute(
                """
                SELECT id, display_name, status, created_at, last_seen_at
                FROM visitors ORDER BY last_seen_at DESC LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
            messages = connection.execute(
                "SELECT * FROM messages WHERE room_id = 'main' ORDER BY room_seq DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
            audits = connection.execute(
                "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
            totals = {
                "visitors": connection.execute(
                    "SELECT COUNT(*) AS count FROM visitors"
                ).fetchone()["count"],
                "messages": connection.execute(
                    "SELECT COUNT(*) AS count FROM messages"
                ).fetchone()["count"],
                "banned": connection.execute(
                    "SELECT COUNT(*) AS count FROM visitors WHERE status = 'banned'"
                ).fetchone()["count"],
            }
        return {
            "visitors": [dict(row) for row in visitors],
            "messages": [
                self._row_to_message(row).as_public_payload() for row in messages
            ],
            "audit": [
                {
                    **{key: row[key] for key in row.keys() if key != "details_json"},
                    "details": _decode_json(row["details_json"], {}),
                }
                for row in audits
            ],
            "totals": totals,
        }

    async def set_visitor_status(
        self, visitor_id: str, status: str, *, actor_id: str = "admin"
    ) -> bool:
        if status not in {"active", "banned"}:
            raise ValueError("invalid visitor status")
        async with self._write_lock:
            return await asyncio.to_thread(
                self._set_visitor_status_sync, visitor_id, status, actor_id
            )

    def _set_visitor_status_sync(
        self, visitor_id: str, status: str, actor_id: str
    ) -> bool:
        with self._managed_connection() as connection:
            result = connection.execute(
                "UPDATE visitors SET status = ? WHERE id = ?",
                (status, visitor_id),
            )
            if result.rowcount:
                self._insert_audit_on(
                    connection, actor_id, "visitor.status", "visitor", visitor_id,
                    {"status": status},
                )
            return bool(result.rowcount)

    async def moderate_message(
        self, message_id: str, status: str, *, actor_id: str = "admin"
    ) -> dict[str, Any] | None:
        if status not in {"visible", "hidden"}:
            raise ValueError("invalid message status")
        async with self._write_lock:
            return await asyncio.to_thread(
                self._moderate_message_sync, message_id, status, actor_id
            )

    def _moderate_message_sync(
        self, message_id: str, status: str, actor_id: str
    ) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            connection.execute(
                "UPDATE messages SET status = ? WHERE id = ?", (status, message_id)
            )
            message = self._message_by_id_on(
                connection, message_id
            ).as_public_payload()
            seq = self._next_seq_on(connection, row["room_id"])
            event = self._insert_event_on(
                connection,
                room_id=row["room_id"],
                room_seq=seq,
                event_type="message.moderated",
                payload={
                    "message_id": message_id,
                    "status": status,
                    "message": message if status == "visible" else None,
                },
            )
            self._insert_audit_on(
                connection, actor_id, "message.moderate", "message", message_id,
                {"status": status},
            )
            connection.commit()
            return event
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def audit(
        self,
        action: str,
        target_type: str,
        target_id: str,
        details: dict[str, Any] | None = None,
        *,
        actor_id: str = "admin",
    ) -> None:
        async with self._write_lock:
            await asyncio.to_thread(
                self._audit_sync,
                actor_id,
                action,
                target_type,
                target_id,
                details or {},
            )

    def _audit_sync(
        self, actor_id: str, action: str, target_type: str,
        target_id: str, details: dict[str, Any]
    ) -> None:
        with self._managed_connection() as connection:
            self._insert_audit_on(
                connection, actor_id, action, target_type, target_id, details
            )

    @staticmethod
    def _insert_audit_on(
        connection: _Connection,
        actor_id: str,
        action: str,
        target_type: str,
        target_id: str,
        details: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_log(id, actor_id, action, target_type, target_id, details_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?::jsonb, ?)
            """,
            (
                f"audit_{uuid4().hex}", actor_id, action, target_type, target_id,
                json.dumps(details, ensure_ascii=False), utc_now(),
            ),
        )

    async def get_setting(self, key: str, default: Any = None) -> Any:
        return await asyncio.to_thread(self._get_setting_sync, key, default)

    def _get_setting_sync(self, key: str, default: Any) -> Any:
        with self._managed_connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM service_settings WHERE key = ?", (key,)
            ).fetchone()
        return default if row is None else _decode_json(row["value_json"], default)

    async def set_setting(
        self, key: str, value: Any, *, actor_id: str = "admin"
    ) -> None:
        async with self._write_lock:
            await asyncio.to_thread(
                self._set_setting_sync, key, value, actor_id
            )

    def _set_setting_sync(self, key: str, value: Any, actor_id: str) -> None:
        with self._managed_connection() as connection:
            connection.execute(
                """
                INSERT INTO service_settings(key, value_json, updated_at) VALUES(?, ?::jsonb, ?)
                ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False), utc_now()),
            )
            self._insert_audit_on(
                connection, actor_id, "settings.update", "setting", key, {"value": value}
            )

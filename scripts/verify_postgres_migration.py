"""Verify guarded SQLite-to-PostgreSQL migration and semantic evidence."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.migrate_sqlite_to_postgres import MigrationError, migrate
from scripts.verification_postgres import connect, reset_public_tables


LEGACY_SCHEMA = """
CREATE TABLE visitors (
    id TEXT PRIMARY KEY, display_name TEXT NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE TABLE rooms (
    id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
    last_seq INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE messages (
    id TEXT PRIMARY KEY, room_id TEXT NOT NULL, room_seq INTEGER NOT NULL,
    author_type TEXT NOT NULL, author_id TEXT NOT NULL, display_name TEXT NOT NULL,
    reply_to_id TEXT, content TEXT NOT NULL, status TEXT NOT NULL,
    metadata_json TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(room_id, room_seq), FOREIGN KEY(room_id) REFERENCES rooms(id)
);
CREATE TABLE room_events (
    id TEXT PRIMARY KEY, room_id TEXT NOT NULL, room_seq INTEGER NOT NULL,
    type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(room_id, room_seq), FOREIGN KEY(room_id) REFERENCES rooms(id)
);
CREATE TABLE client_requests (
    visitor_id TEXT NOT NULL, request_id TEXT NOT NULL, result_json TEXT NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY(visitor_id, request_id)
);
CREATE TABLE turns (
    id TEXT PRIMARY KEY, room_id TEXT NOT NULL, target_visitor_id TEXT,
    source_message_ids_json TEXT NOT NULL, reason_code TEXT NOT NULL,
    decision_json TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
    completed_at TEXT, error_code TEXT
);
CREATE TABLE audit_log (
    id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, action TEXT NOT NULL,
    target_type TEXT NOT NULL, target_id TEXT NOT NULL, details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE service_settings (
    key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


def _create_fixture(path: Path) -> None:
    timestamp = "2026-08-16T00:00:00Z"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(LEGACY_SCHEMA)
        connection.execute(
            "INSERT INTO visitors VALUES(?, ?, ?, ?, ?)",
            ("vis_fixture", "迁移访客", "active", timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO rooms VALUES(?, ?, ?, ?, ?, ?)",
            ("main", "main", "active", 3, timestamp, timestamp),
        )
        visitor_payload = {
            "id": "msg_user",
            "room_id": "main",
            "room_seq": 1,
            "author_type": "visitor",
            "display_name": "迁移访客",
            "reply_to_id": None,
            "content": "迁移内容",
            "status": "visible",
            "created_at": timestamp,
        }
        assistant_payload = {
            "id": "msg_neko",
            "room_id": "main",
            "room_seq": 3,
            "author_type": "neko",
            "display_name": "NEKO",
            "reply_to_id": "msg_user",
            "content": "迁移完成",
            "status": "visible",
            "created_at": timestamp,
        }
        connection.execute(
            "INSERT INTO messages VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "msg_user",
                "main",
                1,
                "visitor",
                "vis_fixture",
                "迁移访客",
                None,
                "迁移内容",
                "visible",
                "{}",
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO messages VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "msg_neko",
                "main",
                3,
                "neko",
                "character:NEKO",
                "NEKO",
                "msg_user",
                "迁移完成",
                "visible",
                json.dumps(
                    {"target_visitor_id": "vis_fixture", "emotion": "joy"},
                    ensure_ascii=False,
                ),
                timestamp,
            ),
        )
        for event_id, sequence, event_type, payload in (
            ("evt_1", 1, "message.created", visitor_payload),
            ("evt_2", 2, "turn.started", {"turn_id": "turn_1"}),
            ("evt_3", 3, "message.created", assistant_payload),
        ):
            connection.execute(
                "INSERT INTO room_events VALUES(?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    "main",
                    sequence,
                    event_type,
                    json.dumps(payload, ensure_ascii=False),
                    timestamp,
                ),
            )
        connection.execute(
            "INSERT INTO client_requests VALUES(?, ?, ?, ?)",
            (
                "vis_fixture",
                "req_fixture",
                json.dumps(
                    {"message_id": "msg_user", "event": visitor_payload},
                    ensure_ascii=False,
                ),
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO turns VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "turn_1",
                "main",
                "vis_fixture",
                '["msg_user"]',
                "direct_mention",
                '{"score": 10}',
                "completed",
                timestamp,
                timestamp,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO audit_log VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                "audit_1",
                "admin",
                "migration.fixture",
                "room",
                "main",
                '{"verified": true}',
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO service_settings VALUES(?, ?, ?)",
            ("room_controls", '{"read_only": false}', timestamp),
        )
        connection.commit()
    finally:
        connection.close()


def main() -> None:
    reset_public_tables()
    workspace_var = ROOT / "var"
    workspace_var.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="postgres-migration-verify-", dir=workspace_var
    ) as temporary:
        root = Path(temporary)
        source = root / "public-room.db"
        _create_fixture(source)

        dry_run = migrate(source, data_dir=root, dry_run=True)
        assert dry_run["ok"] is True
        assert dry_run["dry_run"] is True
        assert dry_run["counts"]["messages"] == 2

        report = migrate(source, data_dir=root)
        assert report["ok"] is True
        assert report["dry_run"] is False
        assert report["counts"] == {
            "visitors": 1,
            "rooms": 1,
            "messages": 2,
            "room_events": 3,
            "client_requests": 1,
            "turns": 1,
            "audit_log": 1,
            "service_settings": 1,
        }
        assert report["room_last_seq"] == {"main": 3}
        assert all(len(value) == 64 for value in report["fingerprints"].values())

        with connect() as target:
            assistant = target.execute(
                "SELECT content, metadata_json FROM messages WHERE id = %s",
                ("msg_neko",),
            ).fetchone()
            assert assistant["content"] == "迁移完成"
            assert assistant["metadata_json"] == {
                "target_visitor_id": "vis_fixture",
                "emotion": "joy",
            }
            assert target.execute(
                "SELECT last_seq FROM rooms WHERE id = 'main'"
            ).fetchone()["last_seq"] == 3

        try:
            migrate(source, data_dir=root)
        except MigrationError as exc:
            assert "target is not empty" in str(exc)
        else:
            raise AssertionError("migration accepted a non-empty PostgreSQL target")

        invalid = root / "invalid.db"
        invalid.write_text("not sqlite", encoding="utf-8")
        try:
            migrate(invalid, data_dir=root)
        except MigrationError as exc:
            assert "not a SQLite" in str(exc)
        else:
            raise AssertionError("migration accepted a non-SQLite source")

    print("PostgreSQL migration verification passed")


if __name__ == "__main__":
    main()

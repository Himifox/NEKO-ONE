"""Deterministic backup/restore verification without production data."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from manage_backup import (
    BackupError,
    MANIFEST_DIGEST_NAME,
    MANIFEST_NAME,
    create_backup,
    restore_backup,
    restore_postgres_backup,
    verify_backup,
)
from main_logic.room.store import RoomStore
from verification_postgres import connect, database_url, reset_public_tables


def _write_manifest_digest(directory: Path) -> None:
    manifest = directory / MANIFEST_NAME
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    (directory / MANIFEST_DIGEST_NAME).write_text(
        f"{digest}  {MANIFEST_NAME}\n", encoding="ascii"
    )


def _expect_failure(operation, phrase: str) -> None:
    try:
        operation()
    except BackupError as exc:
        assert phrase in str(exc), (phrase, str(exc))
    else:
        raise AssertionError(f"operation unexpectedly succeeded; wanted {phrase!r}")


def main() -> None:
    workspace_var = Path(__file__).resolve().parents[1] / "var"
    workspace_var.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="backup-verify-", dir=workspace_var))
    public = temporary / "source-public"
    memory = temporary / "source-memory"
    private = temporary / "source-private"
    backup = temporary / "snapshot"
    restore = temporary / "isolated-restore"
    public.mkdir()
    memory.mkdir()
    private.mkdir()
    reset_public_tables()
    restore_database_url = os.environ.get("NEKO_POSTGRES_RESTORE_URL", "").strip()
    if not restore_database_url.startswith(("postgresql://", "postgres://")):
        raise RuntimeError("NEKO_POSTGRES_RESTORE_URL is required")
    try:
        (public / "session.secret").write_text("guest-secret", encoding="utf-8")
        (public / "admin-session.secret").write_text("admin-secret", encoding="utf-8")
        speech = public / "speech"
        speech.mkdir()
        (speech / "reply.wav").write_bytes(b"RIFF deterministic-audio")
        (memory / "persona.json").write_text(
            json.dumps({"persona": "NEKO", "version": "persona-v7"}),
            encoding="utf-8",
        )
        (private / "providers.env").write_text("API_KEY=private-value\n", encoding="utf-8")

        asyncio.run(RoomStore(database_url(), data_dir=public).initialize())
        with connect() as connection:
            timestamp = "2026-08-16T00:00:00Z"
            connection.execute(
                "UPDATE rooms SET last_seq = 7, updated_at = %s WHERE id = 'main'",
                (timestamp,),
            )
            connection.execute(
                """
                INSERT INTO messages(
                    id, room_id, room_seq, author_type, author_id, display_name,
                    reply_to_id, content, status, metadata_json, created_at
                ) VALUES(
                    'msg-1', 'main', 1, 'neko', 'character:NEKO', 'NEKO',
                    NULL, 'before backup', 'visible', '{}'::jsonb, %s
                )
                """,
                (timestamp,),
            )
            connection.execute(
                """
                INSERT INTO room_events(id, room_id, room_seq, type, payload_json, created_at)
                VALUES(
                    'evt-1', 'main', 1, 'message.created',
                    '{"id":"msg-1","content":"before backup"}'::jsonb, %s
                )
                """,
                (timestamp,),
            )

        memory_database = memory / "facts.sqlite3"
        with closing(sqlite3.connect(memory_database)) as memory_connection:
            memory_connection.execute(
                "CREATE TABLE facts(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
            memory_connection.execute("INSERT INTO facts(value) VALUES('visitor likes tea')")

        manifest = create_backup(
            output=backup,
            public_data=public,
            memory_data=memory,
            private_config=private,
            persona_version="persona-v7",
        )
        assert manifest["plaintext"] is True
        assert manifest["encryption_required_before_transfer"] is True
        assert manifest["persona_version"] == "persona-v7"
        assert {source["name"] for source in manifest["sources"]} == {
            "postgresql",
            "public",
            "memory",
            "private-config",
        }
        manifest_paths = {entry["path"] for entry in manifest["files"]}
        assert "data/postgresql/public-room.dump" in manifest_paths
        assert "data/memory/facts.sqlite3" in manifest_paths
        assert not any(path.endswith(("-wal", "-shm", "-journal")) for path in manifest_paths)
        database_entry = next(
            entry
            for entry in manifest["files"]
            if entry["path"] == "data/postgresql/public-room.dump"
        )
        assert database_entry["kind"] == "postgresql"
        assert database_entry["postgresql"]["schema_version"] == 1
        assert database_entry["postgresql"]["room_last_seq"] == {"main": 7}
        assert database_entry["postgresql"]["table_counts"]["messages"] == 1

        verification = verify_backup(backup)
        assert verification["ok"] is True
        assert verification["sqlite_files"] == 1
        assert verification["postgresql_archives"] == 1

        with connect() as connection:
            connection.execute(
                "UPDATE rooms SET last_seq=8 WHERE id='main'"
            )
            connection.execute(
                """
                INSERT INTO messages(
                    id, room_id, room_seq, author_type, author_id, display_name,
                    reply_to_id, content, status, metadata_json, created_at
                ) VALUES(
                    'msg-2', 'main', 8, 'neko', 'character:NEKO', 'NEKO',
                    NULL, 'after backup', 'visible', '{}'::jsonb,
                    '2026-08-16T00:01:00Z'
                )
                """
            )
        (memory / "persona.json").write_text("changed after backup", encoding="utf-8")

        report = restore_backup(backup=backup, destination=restore)
        assert report["ok"] is True
        assert report["layout"]["public"] == "data/public"
        assert report["layout"]["postgresql"] == "data/postgresql"
        assert (restore / "data" / "postgresql" / "public-room.dump").is_file()
        with psycopg.connect(
            restore_database_url, row_factory=dict_row
        ) as restore_connection:
            restore_database_name = restore_connection.execute(
                "SELECT current_database() AS database"
            ).fetchone()["database"]
        postgres_restore = restore_postgres_backup(
            backup=backup,
            database_url=restore_database_url,
            confirm_empty_database=str(restore_database_name),
        )
        assert postgres_restore["ok"] is True
        assert postgres_restore["postgresql"]["room_last_seq"] == {"main": 7}
        with psycopg.connect(
            restore_database_url, row_factory=dict_row
        ) as restored_connection:
            assert restored_connection.execute(
                "SELECT last_seq FROM rooms"
            ).fetchone()["last_seq"] == 7
            assert restored_connection.execute(
                "SELECT COUNT(*) AS count FROM messages"
            ).fetchone()["count"] == 1
            assert restored_connection.execute(
                "SELECT content FROM messages WHERE id = 'msg-1'"
            ).fetchone()["content"] == "before backup"
        restored_persona = restore / "data" / "memory" / "persona.json"
        assert json.loads(restored_persona.read_text(encoding="utf-8"))["version"] == "persona-v7"
        assert (restore / "data" / "private-config" / "providers.env").is_file()
        assert (restore / "restore-report.json").is_file()

        _expect_failure(
            lambda: restore_backup(backup=backup, destination=restore),
            "already exists",
        )
        _expect_failure(
            lambda: restore_postgres_backup(
                backup=backup,
                database_url=restore_database_url,
                confirm_empty_database=str(restore_database_name),
            ),
            "not empty",
        )

        tampered = temporary / "tampered-content"
        shutil.copytree(backup, tampered)
        (tampered / "data" / "public" / "session.secret").write_text(
            "modified", encoding="utf-8"
        )
        _expect_failure(lambda: verify_backup(tampered), "differs from manifest")

        unsafe = temporary / "tampered-path"
        shutil.copytree(backup, unsafe)
        unsafe_manifest_path = unsafe / MANIFEST_NAME
        unsafe_manifest = json.loads(unsafe_manifest_path.read_text(encoding="utf-8"))
        unsafe_manifest["files"][0]["path"] = "../escape"
        unsafe_manifest_path.write_text(
            json.dumps(unsafe_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _write_manifest_digest(unsafe)
        _expect_failure(lambda: verify_backup(unsafe), "unsafe manifest path")

        print(
            "backup/restore verification passed: PostgreSQL dump/restore, online "
            "Memory SQLite snapshot, manifests, corruption detection, and path rejection"
        )
    finally:
        shutil.rmtree(temporary, ignore_errors=False)


if __name__ == "__main__":
    main()

"""Deterministic backup/restore verification without production data."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from manage_backup import (
    BackupError,
    MANIFEST_DIGEST_NAME,
    MANIFEST_NAME,
    create_backup,
    restore_backup,
    verify_backup,
)


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
    connection: sqlite3.Connection | None = None
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

        database = public / "public-room.db"
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE rooms(id TEXT PRIMARY KEY, last_seq INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE messages(id TEXT PRIMARY KEY, content TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO rooms VALUES('main', 7)")
        connection.execute("INSERT INTO messages VALUES('msg-1', 'before backup')")
        connection.commit()

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
            "public",
            "memory",
            "private-config",
        }
        manifest_paths = {entry["path"] for entry in manifest["files"]}
        assert "data/public/public-room.db" in manifest_paths
        assert "data/memory/facts.sqlite3" in manifest_paths
        assert not any(path.endswith(("-wal", "-shm", "-journal")) for path in manifest_paths)
        database_entry = next(
            entry for entry in manifest["files"] if entry["path"] == "data/public/public-room.db"
        )
        assert database_entry["kind"] == "sqlite"
        assert database_entry["sqlite"]["integrity_check"] == "ok"
        assert database_entry["sqlite"]["room_last_seq"] == {"main": 7}

        verification = verify_backup(backup)
        assert verification["ok"] is True
        assert verification["sqlite_files"] == 2

        connection.execute("INSERT INTO messages VALUES('msg-2', 'after backup')")
        connection.execute("UPDATE rooms SET last_seq=8 WHERE id='main'")
        connection.commit()
        (memory / "persona.json").write_text("changed after backup", encoding="utf-8")

        report = restore_backup(backup=backup, destination=restore)
        assert report["ok"] is True
        assert report["layout"]["public"] == "data/public"
        restored_database = restore / "data" / "public" / "public-room.db"
        with closing(sqlite3.connect(restored_database)) as restored_connection:
            assert restored_connection.execute("SELECT last_seq FROM rooms").fetchone()[0] == 7
            assert restored_connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
            assert restored_connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        restored_persona = restore / "data" / "memory" / "persona.json"
        assert json.loads(restored_persona.read_text(encoding="utf-8"))["version"] == "persona-v7"
        assert (restore / "data" / "private-config" / "providers.env").is_file()
        assert (restore / "restore-report.json").is_file()

        _expect_failure(
            lambda: restore_backup(backup=backup, destination=restore),
            "already exists",
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
            "backup/restore verification passed: online SQLite snapshot, manifests, "
            "isolated restore, corruption detection, and path rejection"
        )
    finally:
        if connection is not None:
            connection.close()
        shutil.rmtree(temporary, ignore_errors=False)


if __name__ == "__main__":
    main()

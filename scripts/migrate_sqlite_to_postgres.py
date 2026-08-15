"""One-time, verified migration of the legacy public RoomStore to PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Mapping

import psycopg
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main_logic.room.store import RoomStore, SCHEMA_VERSION


class MigrationError(RuntimeError):
    pass


TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "visitors": ("id", "display_name", "status", "created_at", "last_seen_at"),
    "rooms": ("id", "slug", "status", "last_seq", "created_at", "updated_at"),
    "messages": (
        "id",
        "room_id",
        "room_seq",
        "author_type",
        "author_id",
        "display_name",
        "reply_to_id",
        "content",
        "status",
        "metadata_json",
        "created_at",
    ),
    "room_events": (
        "id",
        "room_id",
        "room_seq",
        "type",
        "payload_json",
        "created_at",
    ),
    "client_requests": ("visitor_id", "request_id", "result_json", "created_at"),
    "turns": (
        "id",
        "room_id",
        "target_visitor_id",
        "source_message_ids_json",
        "reason_code",
        "decision_json",
        "status",
        "started_at",
        "completed_at",
        "error_code",
    ),
    "audit_log": (
        "id",
        "actor_id",
        "action",
        "target_type",
        "target_id",
        "details_json",
        "created_at",
    ),
    "service_settings": ("key", "value_json", "updated_at"),
}

PRIMARY_ORDER: dict[str, tuple[str, ...]] = {
    "visitors": ("id",),
    "rooms": ("id",),
    "messages": ("id",),
    "room_events": ("id",),
    "client_requests": ("visitor_id", "request_id"),
    "turns": ("id",),
    "audit_log": ("id",),
    "service_settings": ("key",),
}

JSON_COLUMNS = {
    "metadata_json",
    "payload_json",
    "result_json",
    "source_message_ids_json",
    "decision_json",
    "details_json",
    "value_json",
}


def _database_url() -> str:
    value = os.environ.get("NEKO_PUBLIC_DATABASE_URL", "").strip()
    if not value.startswith(("postgresql://", "postgres://")):
        raise MigrationError("NEKO_PUBLIC_DATABASE_URL must point to PostgreSQL")
    return value


def _source_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise MigrationError(f"SQLite source does not exist: {path}")
    with path.open("rb") as source_file:
        header = source_file.read(16)
    if header != b"SQLite format 3\x00":
        raise MigrationError("source is not a SQLite database")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _decode_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise MigrationError("source contains invalid JSON") from exc
    return value


def _normalize_row(row: Mapping[str, Any], columns: Iterable[str]) -> dict[str, Any]:
    return {
        column: _decode_json(row[column]) if column in JSON_COLUMNS else row[column]
        for column in columns
    }


def _fingerprint(rows: Iterable[Mapping[str, Any]], columns: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        normalized = _normalize_row(row, columns)
        digest.update(
            json.dumps(
                normalized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _source_rows(
    connection: sqlite3.Connection, table: str
) -> list[dict[str, Any]]:
    columns = TABLE_COLUMNS[table]
    order = ", ".join(PRIMARY_ORDER[table])
    selected = ", ".join(columns)
    return [
        dict(row)
        for row in connection.execute(
            f"SELECT {selected} FROM {table} ORDER BY {order}"
        ).fetchall()
    ]


def _validate_source(connection: sqlite3.Connection) -> dict[str, Any]:
    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity.lower() != "ok":
        raise MigrationError(f"SQLite integrity_check failed: {integrity}")
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise MigrationError("SQLite foreign_key_check found errors")
    existing = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing = sorted(set(TABLE_COLUMNS) - existing)
    if missing:
        raise MigrationError(f"SQLite source is missing tables: {missing}")

    rows = {table: _source_rows(connection, table) for table in TABLE_COLUMNS}
    room_last_seq = {
        str(row["id"]): int(row["last_seq"]) for row in rows["rooms"]
    }
    if "main" not in room_last_seq:
        raise MigrationError("SQLite source does not contain the main room")
    for room_id, last_seq in room_last_seq.items():
        maximum = connection.execute(
            "SELECT COALESCE(MAX(room_seq), 0) FROM room_events WHERE room_id = ?",
            (room_id,),
        ).fetchone()[0]
        if int(maximum) > last_seq:
            raise MigrationError(f"room {room_id} last_seq is behind its events")
    return {
        "rows": rows,
        "counts": {table: len(values) for table, values in rows.items()},
        "fingerprints": {
            table: _fingerprint(values, TABLE_COLUMNS[table])
            for table, values in rows.items()
        },
        "room_last_seq": room_last_seq,
    }


def _connect_postgres() -> psycopg.Connection:
    return psycopg.connect(
        _database_url(),
        row_factory=dict_row,
        application_name="neko-one-sqlite-migration",
        options=(
            "-c statement_timeout=0 "
            "-c lock_timeout=5000 "
            "-c idle_in_transaction_session_timeout=60000"
        ),
    )


def _target_rows(
    connection: psycopg.Connection, table: str
) -> list[dict[str, Any]]:
    columns = TABLE_COLUMNS[table]
    selected = ", ".join(columns)
    order = ", ".join(PRIMARY_ORDER[table])
    return [
        dict(row)
        for row in connection.execute(
            f"SELECT {selected} FROM {table} ORDER BY {order}"
        ).fetchall()
    ]


def _assert_empty_target(connection: psycopg.Connection) -> None:
    nonempty: dict[str, int] = {}
    for table in TABLE_COLUMNS:
        count = int(
            connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()[
                "count"
            ]
        )
        if count:
            nonempty[table] = count
    if nonempty == {"rooms": 1}:
        default_room = connection.execute(
            "SELECT id, slug, status, last_seq FROM rooms"
        ).fetchone()
        if default_room == {
            "id": "main",
            "slug": "main",
            "status": "active",
            "last_seq": 0,
        }:
            return
    if nonempty:
        raise MigrationError(f"PostgreSQL target is not empty: {nonempty}")


def _insert_rows(
    connection: psycopg.Connection,
    table: str,
    rows: list[dict[str, Any]],
) -> None:
    if not rows:
        return
    columns = TABLE_COLUMNS[table]
    placeholders = [
        "%s::jsonb" if column in JSON_COLUMNS else "%s" for column in columns
    ]
    query = (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join(placeholders)})"
    )
    values = []
    for row in rows:
        values.append(
            tuple(
                json.dumps(_decode_json(row[column]), ensure_ascii=False)
                if column in JSON_COLUMNS
                else row[column]
                for column in columns
            )
        )
    connection.executemany(query, values)


def _target_evidence(connection: psycopg.Connection) -> dict[str, Any]:
    rows = {table: _target_rows(connection, table) for table in TABLE_COLUMNS}
    return {
        "counts": {table: len(values) for table, values in rows.items()},
        "fingerprints": {
            table: _fingerprint(values, TABLE_COLUMNS[table])
            for table, values in rows.items()
        },
        "room_last_seq": {
            str(row["id"]): int(row["last_seq"]) for row in rows["rooms"]
        },
    }


def migrate(source: Path, *, data_dir: Path, dry_run: bool = False) -> dict[str, Any]:
    with closing(_source_connection(source)) as source_connection:
        source_evidence = _validate_source(source_connection)

    asyncio.run(RoomStore(_database_url(), data_dir=data_dir).initialize())
    with closing(_connect_postgres()) as target:
        try:
            target.execute(
                "LOCK TABLE " + ", ".join(TABLE_COLUMNS) + " IN ACCESS EXCLUSIVE MODE"
            )
            _assert_empty_target(target)
            if dry_run:
                target.rollback()
                return {
                    "ok": True,
                    "dry_run": True,
                    "schema_version": SCHEMA_VERSION,
                    "source": str(source.resolve()),
                    "counts": source_evidence["counts"],
                    "fingerprints": source_evidence["fingerprints"],
                    "room_last_seq": source_evidence["room_last_seq"],
                }

            target.execute("DELETE FROM rooms")
            for table in TABLE_COLUMNS:
                _insert_rows(target, table, source_evidence["rows"][table])
            target_evidence = _target_evidence(target)
            for field in ("counts", "fingerprints", "room_last_seq"):
                if target_evidence[field] != source_evidence[field]:
                    raise MigrationError(f"post-import {field} verification failed")
            target.commit()
        except Exception:
            target.rollback()
            raise

    return {
        "ok": True,
        "dry_run": False,
        "schema_version": SCHEMA_VERSION,
        "source": str(source.resolve()),
        "counts": source_evidence["counts"],
        "fingerprints": source_evidence["fingerprints"],
        "room_last_seq": source_evidence["room_last_seq"],
    }


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=ROOT / "var" / "migration-runtime",
        help="local runtime directory used only for storage readiness",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = arguments()
    report = migrate(args.source, data_dir=args.data_dir, dry_run=args.dry_run)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()

"""Create, verify, and restore NEKO-ONE data snapshots.

The public-room PostgreSQL database is captured with ``pg_dump``. Memory-owned
SQLite files use SQLite's online backup API. Other files are copied only after
their size, mtime, and content hash remain stable. The result is a plaintext
staging snapshot: operators must encrypt it before it leaves the trusted host.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from main_logic.room.store import SCHEMA_VERSION


FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
MANIFEST_DIGEST_NAME = "manifest.sha256"
SQLITE_HEADER = b"SQLite format 3\x00"
COPY_ATTEMPTS = 3
PUBLISH_ATTEMPTS = 5
ROOT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
POSTGRES_ROOT = "postgresql"
POSTGRES_DUMP_NAME = "public-room.dump"
PUBLIC_TABLES = (
    "visitors",
    "rooms",
    "messages",
    "room_events",
    "client_requests",
    "turns",
    "audit_log",
    "service_settings",
)


class BackupError(RuntimeError):
    """Raised when a safe snapshot or restore cannot be proven."""


@dataclass(frozen=True)
class SourceRoot:
    name: str
    path: Path
    required: bool = True


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _publish_directory(temporary: Path, destination: Path) -> None:
    """Atomically publish a directory, tolerating short Windows scanner locks."""

    for attempt in range(1, PUBLISH_ATTEMPTS + 1):
        try:
            os.replace(temporary, destination)
            return
        except PermissionError:
            if attempt == PUBLISH_ATTEMPTS:
                raise
            time.sleep(0.1 * attempt)


def _is_sqlite(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open("rb") as stream:
            return stream.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError as exc:
        raise BackupError(f"cannot inspect file {path}: {exc}") from exc


def _is_sqlite_sidecar(path: Path, sqlite_files: set[Path]) -> bool:
    name = path.name
    for suffix in ("-wal", "-shm", "-journal"):
        if name.endswith(suffix) and path.with_name(name[: -len(suffix)]) in sqlite_files:
            return True
    return False


def _validate_tree_path(path: Path) -> None:
    current = path
    while True:
        if current.is_symlink():
            raise BackupError(f"symbolic links are not allowed in backup sources: {current}")
        parent = current.parent
        if parent == current:
            break
        current = parent


def _inventory(source: Path) -> tuple[list[Path], set[Path]]:
    _validate_tree_path(source)
    if source.is_file():
        files = [source]
    elif source.is_dir():
        files = []
        for candidate in source.rglob("*"):
            if candidate.is_symlink():
                raise BackupError(
                    f"symbolic links are not allowed in backup sources: {candidate}"
                )
            if candidate.is_file():
                files.append(candidate)
    else:
        raise BackupError(f"backup source is not a regular file or directory: {source}")
    sqlite_files = {path for path in files if _is_sqlite(path)}
    visible = sorted(
        (path for path in files if not _is_sqlite_sidecar(path, sqlite_files)),
        key=lambda item: item.as_posix(),
    )
    return visible, sqlite_files


def _relative_path(source: Path, candidate: Path) -> Path:
    return Path(candidate.name) if source.is_file() else candidate.relative_to(source)


def _copy_stable_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, COPY_ATTEMPTS + 1):
        before = source.stat()
        shutil.copy2(source, destination)
        source_digest = _sha256(source)
        destination_digest = _sha256(destination)
        after = source.stat()
        stable = (
            before.st_size == after.st_size
            and before.st_mtime_ns == after.st_mtime_ns
            and source_digest == destination_digest
        )
        if stable:
            return
        if attempt < COPY_ATTEMPTS:
            time.sleep(0.05 * attempt)
    raise BackupError(f"file changed while being copied: {source}")


def _sqlite_metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity.lower() != "ok":
        raise BackupError(f"SQLite integrity_check failed: {integrity}")
    foreign_key_errors = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    if foreign_key_errors:
        raise BackupError(f"SQLite foreign_key_check found {foreign_key_errors} errors")
    tables = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        if not str(row[0]).startswith("sqlite_")
    ]
    metadata: dict[str, Any] = {
        "integrity_check": "ok",
        "foreign_key_errors": 0,
        "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        "tables": tables,
    }
    if "rooms" in tables:
        rows = connection.execute("SELECT id, last_seq FROM rooms ORDER BY id").fetchall()
        metadata["room_last_seq"] = {str(row[0]): int(row[1]) for row in rows}
    return metadata


def _backup_sqlite(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    uri = f"{source.resolve().as_uri()}?mode=ro"
    try:
        with closing(
            sqlite3.connect(uri, uri=True, timeout=30.0)
        ) as source_connection:
            with closing(sqlite3.connect(destination)) as destination_connection:
                source_connection.backup(destination_connection)
                destination_connection.commit()
                # A backup inherits the source's WAL mode. Convert the closed,
                # self-contained snapshot to DELETE mode so verification and
                # transfer never depend on unlisted -wal/-shm sidecars.
                journal_mode = str(
                    destination_connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                ).lower()
                if journal_mode != "delete":
                    raise BackupError(
                        f"could not make SQLite snapshot self-contained: {journal_mode}"
                    )
                metadata = _sqlite_metadata(destination_connection)
    except (OSError, sqlite3.Error) as exc:
        raise BackupError(f"SQLite online backup failed for {source}: {exc}") from exc
    return metadata


def _postgres_url(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value.startswith(("postgresql://", "postgres://")):
        raise BackupError(f"{name} must contain a PostgreSQL URL")
    return value


def _postgres_metadata(database_url: str) -> dict[str, Any]:
    try:
        with psycopg.connect(
            database_url,
            row_factory=dict_row,
            connect_timeout=5,
            application_name="neko-one-backup-metadata",
        ) as connection:
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
            version = int(version_row["version"] if version_row else 0)
            if version != SCHEMA_VERSION:
                raise BackupError(
                    f"unsupported PostgreSQL schema version: {version}"
                )
            counts = {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) AS count FROM {table}"
                    ).fetchone()["count"]
                )
                for table in PUBLIC_TABLES
            }
            room_last_seq = {
                str(row["id"]): int(row["last_seq"])
                for row in connection.execute(
                    "SELECT id, last_seq FROM rooms ORDER BY id"
                ).fetchall()
            }
            event_high_water = {
                str(row["room_id"]): int(row["high_water"])
                for row in connection.execute(
                    """
                    SELECT room_id, COALESCE(MAX(room_seq), 0) AS high_water
                    FROM room_events GROUP BY room_id ORDER BY room_id
                    """
                ).fetchall()
            }
    except BackupError:
        raise
    except psycopg.Error as exc:
        raise BackupError("cannot read PostgreSQL backup metadata") from exc
    for room_id, high_water in event_high_water.items():
        if high_water > room_last_seq.get(room_id, -1):
            raise BackupError(f"room {room_id} last_seq is behind persisted events")
    return {
        "schema_version": version,
        "table_counts": counts,
        "room_last_seq": room_last_seq,
        "event_high_water": event_high_water,
    }


def _postgres_environment(database_url: str) -> dict[str, str]:
    environment = os.environ.copy()
    parameters = conninfo_to_dict(database_url)
    mappings = {
        "dbname": "PGDATABASE",
        "host": "PGHOST",
        "port": "PGPORT",
        "user": "PGUSER",
        "password": "PGPASSWORD",
        "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT",
        "sslcert": "PGSSLCERT",
        "sslkey": "PGSSLKEY",
    }
    for parameter, variable in mappings.items():
        value = parameters.get(parameter)
        if value is not None:
            environment[variable] = str(value)
    environment["PGCONNECT_TIMEOUT"] = "10"
    return environment


def _run_postgres_tool(
    command: list[str], *, database_url: str | None = None, timeout: int = 300
) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(command[0])
    if executable is None:
        raise BackupError(f"required PostgreSQL tool is unavailable: {command[0]}")
    resolved = [executable, *command[1:]]
    try:
        return subprocess.run(
            resolved,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=(
                _postgres_environment(database_url)
                if database_url is not None
                else None
            ),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupError(f"{command[0]} failed") from exc


def _verify_postgres_archive(path: Path) -> None:
    result = _run_postgres_tool(["pg_restore", "--list", str(path)], timeout=60)
    listing = result.stdout
    required = ("TABLE public rooms", "TABLE public messages", "TABLE public room_events")
    if any(token not in listing for token in required):
        raise BackupError("PostgreSQL archive is missing required public-room tables")


def _backup_postgres(database_url: str, data_root: Path) -> dict[str, Any]:
    destination_root = data_root / POSTGRES_ROOT
    destination_root.mkdir(parents=True, exist_ok=False)
    destination = destination_root / POSTGRES_DUMP_NAME
    before = _postgres_metadata(database_url)
    _run_postgres_tool(
        [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-acl",
            "--file",
            str(destination),
        ],
        database_url=database_url,
    )
    _verify_postgres_archive(destination)
    after = _postgres_metadata(database_url)
    # pg_dump has its own transaction snapshot. Requiring stable high-level
    # metadata prevents a busy source from producing ambiguous drill evidence.
    if before != after:
        raise BackupError("PostgreSQL metadata changed during snapshot; retry")
    return {
        "path": (Path("data") / POSTGRES_ROOT / POSTGRES_DUMP_NAME).as_posix(),
        "root": POSTGRES_ROOT,
        "kind": "postgresql",
        "size": destination.stat().st_size,
        "sha256": _sha256(destination),
        "postgresql": after,
    }


def _snapshot_source(source: SourceRoot, data_root: Path) -> list[dict[str, Any]]:
    destination_root = data_root / source.name
    last_error: Exception | None = None
    for attempt in range(1, COPY_ATTEMPTS + 1):
        if destination_root.exists():
            shutil.rmtree(destination_root)
        destination_root.mkdir(parents=True, exist_ok=False)
        try:
            before_files, sqlite_files = _inventory(source.path)
            entries: list[dict[str, Any]] = []
            for candidate in before_files:
                relative = _relative_path(source.path, candidate)
                destination = destination_root / relative
                sqlite_metadata: dict[str, Any] | None = None
                if candidate in sqlite_files:
                    sqlite_metadata = _backup_sqlite(candidate, destination)
                    kind = "sqlite"
                else:
                    _copy_stable_file(candidate, destination)
                    kind = "file"
                entry: dict[str, Any] = {
                    "path": (Path("data") / source.name / relative).as_posix(),
                    "root": source.name,
                    "kind": kind,
                    "size": destination.stat().st_size,
                    "sha256": _sha256(destination),
                }
                if sqlite_metadata is not None:
                    entry["sqlite"] = sqlite_metadata
                entries.append(entry)
            after_files, _ = _inventory(source.path)
            before_names = {_relative_path(source.path, path).as_posix() for path in before_files}
            after_names = {_relative_path(source.path, path).as_posix() for path in after_files}
            if before_names != after_names:
                raise BackupError(f"source inventory changed during snapshot: {source.path}")
            return entries
        except (BackupError, OSError, sqlite3.Error) as exc:
            last_error = exc
            if attempt < COPY_ATTEMPTS:
                time.sleep(0.1 * attempt)
    raise BackupError(
        f"could not obtain a stable snapshot of {source.name}: {last_error}"
    ) from last_error


def _app_version(repo_root: Path) -> str:
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        return "unknown"
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            return line.split("=", 1)[1].strip().strip('"')
    return "unknown"


def _git_commit(repo_root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _ensure_non_overlapping(sources: Iterable[SourceRoot], output: Path) -> None:
    resolved = [(source.name, source.path.resolve()) for source in sources]
    for index, (name, path) in enumerate(resolved):
        if output == path or output.is_relative_to(path):
            raise BackupError(f"backup output cannot be inside source {name}: {path}")
        for other_name, other_path in resolved[index + 1 :]:
            if path == other_path or path.is_relative_to(other_path) or other_path.is_relative_to(path):
                raise BackupError(
                    f"backup sources must not overlap: {name}={path}, {other_name}={other_path}"
                )


def create_backup(
    *,
    output: Path,
    public_data: Path,
    database_url: str | None = None,
    memory_data: Path | None = None,
    private_config: Path | None = None,
    persona_version: str = "operator-unset",
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Create one atomic plaintext snapshot and return its manifest."""

    output = output.expanduser().resolve()
    database_url = database_url or _postgres_url("NEKO_PUBLIC_DATABASE_URL")
    sources = [SourceRoot("public", public_data.expanduser().resolve())]
    if memory_data is not None:
        sources.append(SourceRoot("memory", memory_data.expanduser().resolve()))
    if private_config is not None:
        sources.append(SourceRoot("private-config", private_config.expanduser().resolve()))
    for source in sources:
        if not source.path.exists():
            raise BackupError(f"required backup source does not exist: {source.path}")
    _ensure_non_overlapping(sources, output)
    if output.exists():
        raise BackupError(f"backup output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / f".{output.name}.partial-{uuid4().hex}"
    temporary.mkdir(mode=0o700)
    try:
        all_entries: list[dict[str, Any]] = [
            _backup_postgres(database_url, temporary / "data")
        ]
        for source in sources:
            all_entries.extend(_snapshot_source(source, temporary / "data"))
        repo_root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
        manifest: dict[str, Any] = {
            "format": "neko-one-backup",
            "format_version": FORMAT_VERSION,
            "created_at": _utc_now(),
            "application_version": _app_version(repo_root),
            "git_commit": _git_commit(repo_root),
            "persona_version": persona_version.strip() or "operator-unset",
            "plaintext": True,
            "encryption_required_before_transfer": True,
            "sources": [
                {
                    "name": POSTGRES_ROOT,
                    "original_path": "NEKO_PUBLIC_DATABASE_URL (redacted)",
                },
                *[
                {"name": source.name, "original_path": str(source.path)}
                for source in sources
                ],
            ],
            "files": sorted(all_entries, key=lambda entry: entry["path"]),
        }
        manifest_path = temporary / MANIFEST_NAME
        _write_json(manifest_path, manifest)
        (temporary / MANIFEST_DIGEST_NAME).write_text(
            f"{_sha256(manifest_path)}  {MANIFEST_NAME}\n", encoding="ascii"
        )
        try:
            (temporary / MANIFEST_DIGEST_NAME).chmod(0o600)
        except OSError:
            pass
        _publish_directory(temporary, output)
        return manifest
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def _safe_manifest_path(backup: Path, raw_path: str) -> Path:
    pure = PurePosixPath(raw_path)
    if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
        raise BackupError(f"unsafe manifest path: {raw_path!r}")
    candidate = backup.joinpath(*pure.parts).resolve()
    if not candidate.is_relative_to(backup):
        raise BackupError(f"manifest path escapes backup: {raw_path!r}")
    return candidate


def verify_backup(backup: Path) -> dict[str, Any]:
    """Verify manifest, file set, hashes, PostgreSQL archive, and SQLite files."""

    backup = backup.expanduser().resolve()
    _validate_tree_path(backup)
    for candidate in backup.rglob("*"):
        if candidate.is_symlink():
            raise BackupError(f"symbolic links are not allowed in backups: {candidate}")
    manifest_path = backup / MANIFEST_NAME
    digest_path = backup / MANIFEST_DIGEST_NAME
    if not manifest_path.is_file() or not digest_path.is_file():
        raise BackupError("backup manifest or its digest is missing")
    digest_fields = digest_path.read_text(encoding="ascii").strip().split()
    if len(digest_fields) != 2 or digest_fields[1] != MANIFEST_NAME:
        raise BackupError("manifest digest file has an invalid format")
    if digest_fields[0] != _sha256(manifest_path):
        raise BackupError("manifest digest does not match")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"cannot read backup manifest: {exc}") from exc
    if manifest.get("format") != "neko-one-backup" or manifest.get("format_version") != FORMAT_VERSION:
        raise BackupError("unsupported backup format")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise BackupError("manifest sources must be a non-empty list")
    root_names: list[str] = []
    for source in sources:
        name = source.get("name") if isinstance(source, dict) else None
        if not isinstance(name, str) or ROOT_NAME.fullmatch(name) is None:
            raise BackupError(f"manifest contains an invalid root name: {name!r}")
        if name in root_names:
            raise BackupError(f"manifest contains a duplicate root name: {name}")
        root_names.append(name)
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise BackupError("manifest files must be a list")
    expected_paths: set[str] = set()
    sqlite_count = 0
    postgres_count = 0
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise BackupError("manifest contains an invalid file entry")
        raw_path = entry["path"]
        if raw_path in expected_paths:
            raise BackupError(f"duplicate manifest path: {raw_path}")
        expected_paths.add(raw_path)
        path = _safe_manifest_path(backup, raw_path)
        entry_root = entry.get("root")
        pure_path = PurePosixPath(raw_path)
        if (
            entry_root not in root_names
            or len(pure_path.parts) < 3
            or pure_path.parts[:2] != ("data", entry_root)
        ):
            raise BackupError(f"manifest file is outside its declared root: {raw_path}")
        if path.is_symlink() or not path.is_file():
            raise BackupError(f"backup file is missing or unsafe: {raw_path}")
        if path.stat().st_size != entry.get("size"):
            raise BackupError(f"backup file size differs from manifest: {raw_path}")
        if _sha256(path) != entry.get("sha256"):
            raise BackupError(f"backup file hash differs from manifest: {raw_path}")
        if entry.get("kind") == "sqlite":
            sqlite_count += 1
            try:
                with closing(
                    sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
                ) as connection:
                    current_metadata = _sqlite_metadata(connection)
            except sqlite3.Error as exc:
                raise BackupError(f"cannot verify SQLite file {raw_path}: {exc}") from exc
            if current_metadata != entry.get("sqlite"):
                raise BackupError(f"SQLite metadata differs from manifest: {raw_path}")
        elif entry.get("kind") == "postgresql":
            postgres_count += 1
            if entry_root != POSTGRES_ROOT or raw_path != (
                Path("data") / POSTGRES_ROOT / POSTGRES_DUMP_NAME
            ).as_posix():
                raise BackupError("PostgreSQL archive is outside its fixed location")
            metadata = entry.get("postgresql")
            if (
                not isinstance(metadata, dict)
                or metadata.get("schema_version") != SCHEMA_VERSION
                or set(metadata.get("table_counts", {})) != set(PUBLIC_TABLES)
            ):
                raise BackupError("PostgreSQL archive metadata is invalid")
            _verify_postgres_archive(path)
    actual_paths = {
        path.relative_to(backup).as_posix()
        for path in (backup / "data").rglob("*")
        if path.is_file()
    }
    if actual_paths != expected_paths:
        missing = sorted(expected_paths - actual_paths)
        unexpected = sorted(actual_paths - expected_paths)
        raise BackupError(f"backup file set differs: missing={missing}, unexpected={unexpected}")
    if postgres_count != 1:
        raise BackupError("backup must contain exactly one PostgreSQL archive")
    return {
        "ok": True,
        "backup": str(backup),
        "created_at": manifest.get("created_at"),
        "files": len(entries),
        "sqlite_files": sqlite_count,
        "postgresql_archives": postgres_count,
        "bytes": sum(int(entry["size"]) for entry in entries),
        "roots": root_names,
    }


def restore_backup(*, backup: Path, destination: Path) -> dict[str, Any]:
    """Restore only into a new isolation directory, then verify restored data."""

    backup = backup.expanduser().resolve()
    destination = destination.expanduser().resolve()
    verification = verify_backup(backup)
    if destination.exists():
        raise BackupError(f"restore destination already exists: {destination}")
    if destination == backup or destination.is_relative_to(backup):
        raise BackupError("restore destination cannot be inside the backup")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.partial-{uuid4().hex}"
    temporary.mkdir(mode=0o700)
    try:
        shutil.copytree(backup / "data", temporary / "data", copy_function=shutil.copy2)
        manifest = json.loads((backup / MANIFEST_NAME).read_text(encoding="utf-8"))
        for entry in manifest["files"]:
            raw_path = str(entry["path"])
            source_path = _safe_manifest_path(backup, raw_path)
            relative = source_path.relative_to(backup / "data")
            restored_path = temporary / "data" / relative
            if _sha256(restored_path) != entry["sha256"]:
                raise BackupError(f"restored file hash mismatch: {raw_path}")
            if entry.get("kind") == "sqlite":
                with closing(sqlite3.connect(restored_path)) as connection:
                    current_metadata = _sqlite_metadata(connection)
                if current_metadata != entry.get("sqlite"):
                    raise BackupError(f"restored SQLite metadata mismatch: {raw_path}")
            elif entry.get("kind") == "postgresql":
                _verify_postgres_archive(restored_path)
        restore_report = {
            **verification,
            "restored_at": _utc_now(),
            "destination": str(destination),
            "layout": {source["name"]: f"data/{source['name']}" for source in manifest["sources"]},
        }
        _write_json(temporary / "restore-report.json", restore_report)
        _publish_directory(temporary, destination)
        return restore_report
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def restore_postgres_backup(
    *, backup: Path, database_url: str, confirm_empty_database: str
) -> dict[str, Any]:
    """Restore the archive into an explicitly confirmed, completely empty DB."""

    backup = backup.expanduser().resolve()
    verify_backup(backup)
    manifest = json.loads((backup / MANIFEST_NAME).read_text(encoding="utf-8"))
    postgres_entries = [
        entry for entry in manifest["files"] if entry.get("kind") == "postgresql"
    ]
    if len(postgres_entries) != 1:
        raise BackupError("backup does not contain exactly one PostgreSQL archive")
    entry = postgres_entries[0]
    archive = _safe_manifest_path(backup, str(entry["path"]))
    expected_metadata = entry.get("postgresql")

    try:
        with psycopg.connect(
            database_url,
            row_factory=dict_row,
            connect_timeout=5,
            application_name="neko-one-restore-preflight",
        ) as connection:
            identity = connection.execute(
                "SELECT current_database() AS database, current_user AS username"
            ).fetchone()
            database_name = str(identity["database"] if identity else "")
            if not confirm_empty_database or confirm_empty_database != database_name:
                raise BackupError(
                    "--confirm-empty-database must exactly match the restore database"
                )
            tables = [
                str(row["tablename"])
                for row in connection.execute(
                    """
                    SELECT tablename FROM pg_tables
                    WHERE schemaname = 'public' ORDER BY tablename
                    """
                ).fetchall()
            ]
            if tables:
                raise BackupError(
                    f"PostgreSQL restore target is not empty: {tables}"
                )
    except BackupError:
        raise
    except psycopg.Error as exc:
        raise BackupError("cannot inspect PostgreSQL restore target") from exc

    _run_postgres_tool(
        [
            "pg_restore",
            "--exit-on-error",
            "--single-transaction",
            "--no-owner",
            "--no-acl",
            "--dbname",
            database_name,
            str(archive),
        ],
        database_url=database_url,
    )
    restored_metadata = _postgres_metadata(database_url)
    if restored_metadata != expected_metadata:
        raise BackupError(
            "restored PostgreSQL metadata differs; discard the isolated database"
        )
    return {
        "ok": True,
        "backup": str(backup),
        "database": database_name,
        "restored_at": _utc_now(),
        "postgresql": restored_metadata,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create a new plaintext staging snapshot")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--public-data", type=Path, required=True)
    create.add_argument("--memory-data", type=Path)
    create.add_argument("--private-config", type=Path)
    create.add_argument("--persona-version", default="operator-unset")

    verify = subparsers.add_parser("verify", help="verify an existing snapshot")
    verify.add_argument("--backup", type=Path, required=True)

    restore = subparsers.add_parser("restore", help="restore into a new isolation directory")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)

    restore_postgres = subparsers.add_parser(
        "restore-postgres",
        help="restore the PostgreSQL archive into a separately configured empty DB",
    )
    restore_postgres.add_argument("--backup", type=Path, required=True)
    restore_postgres.add_argument("--confirm-empty-database", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            manifest = create_backup(
                output=args.output,
                public_data=args.public_data,
                memory_data=args.memory_data,
                private_config=args.private_config,
                persona_version=args.persona_version,
            )
            result: dict[str, Any] = {
                "ok": True,
                "output": str(args.output.resolve()),
                "files": len(manifest["files"]),
                "plaintext": True,
                "next": "verify, encrypt, and transfer the snapshot",
            }
        elif args.command == "verify":
            result = verify_backup(args.backup)
        elif args.command == "restore":
            result = restore_backup(backup=args.backup, destination=args.destination)
        else:
            result = restore_postgres_backup(
                backup=args.backup,
                database_url=_postgres_url("NEKO_POSTGRES_RESTORE_URL"),
                confirm_empty_database=args.confirm_empty_database,
            )
    except BackupError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

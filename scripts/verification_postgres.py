"""Guarded PostgreSQL helpers used by deterministic verification scripts."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row


PUBLIC_TABLES = (
    "schema_migrations",
    "service_settings",
    "audit_log",
    "turns",
    "client_requests",
    "room_events",
    "messages",
    "rooms",
    "visitors",
)


def database_url() -> str:
    value = os.environ.get("NEKO_PUBLIC_DATABASE_URL", "").strip()
    if not value.startswith(("postgresql://", "postgres://")):
        raise RuntimeError(
            "verification requires NEKO_PUBLIC_DATABASE_URL pointing to PostgreSQL"
        )
    return value


@contextmanager
def connect(*, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    connection = psycopg.connect(
        database_url(),
        autocommit=autocommit,
        row_factory=dict_row,
        application_name="neko-one-verification",
    )
    try:
        yield connection
        if not autocommit:
            connection.commit()
    except Exception:
        if not autocommit:
            connection.rollback()
        raise
    finally:
        connection.close()


def reset_public_tables() -> None:
    """Drop only NEKO public tables, and only behind an explicit CI/local gate."""

    if os.environ.get("NEKO_VERIFY_ALLOW_DATABASE_RESET") != "1":
        raise RuntimeError("NEKO_VERIFY_ALLOW_DATABASE_RESET=1 is required")
    with connect(autocommit=True) as connection:
        identity = connection.execute(
            "SELECT current_database() AS database, current_user AS username"
        ).fetchone()
        database_name = str(identity["database"] if identity else "")
        safe_name = database_name.lower()
        if not any(token in safe_name for token in ("test", "verify", "ci")):
            raise RuntimeError(
                "refusing to reset a database whose name lacks test/verify/ci"
            )
        quoted = ", ".join(PUBLIC_TABLES)
        connection.execute(f"DROP TABLE IF EXISTS {quoted} CASCADE")


def scalar(query: str, params: tuple = ()):
    with connect() as connection:
        row = connection.execute(query, params).fetchone()
    if row is None or len(row) != 1:
        raise RuntimeError("scalar query returned an unexpected row")
    return next(iter(row.values()))

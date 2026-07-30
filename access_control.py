"""Persistent invite and temporary-access storage for the Telegram bot."""

import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

DB_PATH = os.getenv("ACCESS_DB_PATH", "/app/data/access.db")
MOSCOW = timezone(timedelta(hours=3), name="MSK")


def _connect() -> sqlite3.Connection:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """Transaction scope that always closes the SQLite connection."""
    connection = _connect()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_db() -> None:
    with _db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS invites (
                token TEXT PRIMARY KEY,
                duration_days INTEGER NOT NULL,
                created_by INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                activated_by INTEGER,
                activated_at INTEGER,
                expires_at INTEGER
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS access_users (
                chat_id INTEGER PRIMARY KEY,
                display_name TEXT,
                username TEXT,
                expires_at INTEGER NOT NULL,
                invite_token TEXT,
                updated_at INTEGER NOT NULL
            )
            """
        )


def format_expiry(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, MOSCOW).strftime("%d.%m.%Y в %H:%M")


def create_invite(created_by: int, duration_days: int = 7) -> str:
    token = secrets.token_urlsafe(18)
    now = int(time.time())
    with _db() as connection:
        connection.execute(
            "INSERT INTO invites(token, duration_days, created_by, created_at) VALUES (?, ?, ?, ?)",
            (token, duration_days, created_by, now),
        )
    return token


def activate_invite(
    token: str,
    chat_id: int,
    display_name: str,
    username: str | None,
) -> tuple[str, int | None]:
    now = int(time.time())
    with _db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        invite = connection.execute(
            "SELECT * FROM invites WHERE token = ?", (token,)
        ).fetchone()
        if invite is None:
            return "invalid", None
        if invite["activated_by"] is not None:
            if int(invite["activated_by"]) == chat_id:
                return "already", int(invite["expires_at"])
            return "used", None

        expires_at = now + int(invite["duration_days"]) * 86400
        connection.execute(
            """
            UPDATE invites
            SET activated_by = ?, activated_at = ?, expires_at = ?
            WHERE token = ? AND activated_by IS NULL
            """,
            (chat_id, now, expires_at, token),
        )
        connection.execute(
            """
            INSERT INTO access_users(
                chat_id, display_name, username, expires_at, invite_token, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                display_name = excluded.display_name,
                username = excluded.username,
                expires_at = excluded.expires_at,
                invite_token = excluded.invite_token,
                updated_at = excluded.updated_at
            """,
            (chat_id, display_name, username, expires_at, token, now),
        )
    return "activated", expires_at


def get_access(chat_id: int) -> sqlite3.Row | None:
    with _db() as connection:
        return connection.execute(
            "SELECT * FROM access_users WHERE chat_id = ?", (chat_id,)
        ).fetchone()


def has_active_access(chat_id: int) -> bool:
    access = get_access(chat_id)
    return bool(access and int(access["expires_at"]) > int(time.time()))


def list_active_users() -> list[sqlite3.Row]:
    now = int(time.time())
    with _db() as connection:
        return connection.execute(
            """
            SELECT * FROM access_users
            WHERE expires_at > ?
            ORDER BY expires_at ASC
            """,
            (now,),
        ).fetchall()


def revoke_access(chat_id: int) -> bool:
    now = int(time.time())
    with _db() as connection:
        cursor = connection.execute(
            "UPDATE access_users SET expires_at = ?, updated_at = ? WHERE chat_id = ?",
            (now, now, chat_id),
        )
    return cursor.rowcount > 0


def extend_access(chat_id: int, days: int = 7) -> int | None:
    now = int(time.time())
    with _db() as connection:
        access = connection.execute(
            "SELECT expires_at FROM access_users WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if access is None:
            return None
        base = max(now, int(access["expires_at"]))
        expires_at = base + days * 86400
        connection.execute(
            "UPDATE access_users SET expires_at = ?, updated_at = ? WHERE chat_id = ?",
            (expires_at, now, chat_id),
        )
    return expires_at

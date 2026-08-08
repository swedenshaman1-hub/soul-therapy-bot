"""Persistent adaptive-learning state for the Soul Therapy Telegram bot."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator


DB_PATH = os.getenv("LEARNING_DB_PATH", "/app/data/learning.db")
COURSE_VERSION = "soul-therapy-v1"
_SCHEMA_READY_PATH: str | None = None

EXPERIENCE_LEVELS = {
    "none": "Изучаю с нуля",
    "familiar": "Знаком с методом",
    "trained": "Проходил обучение",
    "practice": "Уже практикую",
}

LEARNING_GOALS = {
    "basics": "Понять основы",
    "system": "Систематизировать знания",
    "engineering": "Освоить инженерию метода",
    "practice": "Улучшить практическую работу",
}

DELIVERY_FORMATS = {
    "text": "Текст",
    "voice": "Голос",
    "mixed": "Смешанный формат",
}

DAILY_MINUTES = (15, 30, 45, 60)

# These are curriculum routes, not factual claims.  Lesson content is generated
# only from the connected NotebookLM notebooks and cached separately.
LESSONS = (
    {
        "id": "foundations",
        "title": "Основания и задачи метода",
        "objective": "Понять назначение метода, его основные принципы и границы применения.",
    },
    {
        "id": "human_model",
        "title": "Модель человека в Терапии Души",
        "objective": "Разобраться в используемой методом модели человека и взаимосвязях её уровней.",
    },
    {
        "id": "field",
        "title": "Поле и позиция терапевта",
        "objective": "Понять, как в материалах метода описывается Поле и работа терапевта с ним.",
    },
    {
        "id": "slide",
        "title": "Слайд и незавершённое событие",
        "objective": "Освоить понятие слайда, причины его появления и влияние на настоящее.",
    },
    {
        "id": "figures",
        "title": "Фигуры, роли, состояния и динамики",
        "objective": "Научиться различать элементы внутренней структуры исследуемой ситуации.",
    },
    {
        "id": "diagnostics",
        "title": "Диагностика и проверка гипотез",
        "objective": "Понять логику диагностики и способы проверки терапевтических гипотез.",
    },
    {
        "id": "algorithm",
        "title": "Алгоритм терапевтической работы",
        "objective": "Собрать этапы работы в последовательную и воспроизводимую систему.",
    },
    {
        "id": "interventions",
        "title": "Инструменты и интервенции",
        "objective": "Разобраться в назначении инструментов метода и условиях их применения.",
    },
    {
        "id": "completion",
        "title": "Завершение и интеграция результата",
        "objective": "Понять критерии завершения работы и закрепления результата.",
    },
    {
        "id": "practice",
        "title": "Практика, разбор случаев и развитие мастера",
        "objective": "Учиться применять инженерную логику метода к практическим ситуациям.",
    },
)


def _connect() -> sqlite3.Connection:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 20000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    connection = _connect()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_db() -> None:
    global _SCHEMA_READY_PATH
    statements = (
        """
        CREATE TABLE IF NOT EXISTS learning_profiles (
            user_id INTEGER PRIMARY KEY,
            experience TEXT,
            goal TEXT,
            daily_minutes INTEGER,
            delivery_format TEXT,
            onboarding_step TEXT NOT NULL DEFAULT 'experience',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS lesson_progress (
            user_id INTEGER NOT NULL,
            lesson_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'not_started',
            mastery INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            completed_at INTEGER,
            next_review_at INTEGER,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, lesson_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS lesson_sessions (
            user_id INTEGER PRIMARY KEY,
            lesson_id TEXT NOT NULL,
            segment_index INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL,
            lesson_json TEXT NOT NULL,
            score_total INTEGER NOT NULL DEFAULT 0,
            answer_count INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            started_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS lesson_cache (
            cache_key TEXT PRIMARY KEY,
            lesson_id TEXT NOT NULL,
            course_version TEXT NOT NULL,
            lesson_json TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """,
    )
    with _db() as connection:
        for statement in statements:
            connection.execute(statement)
    _SCHEMA_READY_PATH = DB_PATH


def _ensure_initialized() -> None:
    if _SCHEMA_READY_PATH != DB_PATH:
        init_db()


def ensure_profile(user_id: int) -> dict:
    _ensure_initialized()
    now = int(time.time())
    with _db() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO learning_profiles(
                user_id, onboarding_step, created_at, updated_at
            ) VALUES (?, 'experience', ?, ?)
            """,
            (user_id, now, now),
        )
        row = connection.execute(
            "SELECT * FROM learning_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return dict(row)


def get_profile(user_id: int) -> dict | None:
    _ensure_initialized()
    with _db() as connection:
        row = connection.execute(
            "SELECT * FROM learning_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def update_profile(user_id: int, **fields) -> dict:
    allowed = {
        "experience",
        "goal",
        "daily_minutes",
        "delivery_format",
        "onboarding_step",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unsupported profile fields: {sorted(unknown)}")
    ensure_profile(user_id)
    if not fields:
        return get_profile(user_id) or {}
    fields["updated_at"] = int(time.time())
    assignments = ", ".join(f"{name} = ?" for name in fields)
    values = list(fields.values()) + [user_id]
    with _db() as connection:
        connection.execute(
            f"UPDATE learning_profiles SET {assignments} WHERE user_id = ?",
            values,
        )
    return get_profile(user_id) or {}


def profile_complete(profile: dict | None) -> bool:
    return bool(
        profile
        and profile.get("experience") in EXPERIENCE_LEVELS
        and profile.get("goal") in LEARNING_GOALS
        and int(profile.get("daily_minutes") or 0) in DAILY_MINUTES
        and profile.get("delivery_format") in DELIVERY_FORMATS
        and profile.get("onboarding_step") == "complete"
    )


def restart_onboarding(user_id: int) -> dict:
    return update_profile(
        user_id,
        experience=None,
        goal=None,
        daily_minutes=None,
        delivery_format=None,
        onboarding_step="experience",
    )


def get_lesson(lesson_id: str) -> dict | None:
    return next((dict(item) for item in LESSONS if item["id"] == lesson_id), None)


def get_next_lesson(user_id: int) -> dict | None:
    _ensure_initialized()
    with _db() as connection:
        completed = {
            row["lesson_id"]
            for row in connection.execute(
                "SELECT lesson_id FROM lesson_progress WHERE user_id = ? AND status = 'completed'",
                (user_id,),
            ).fetchall()
        }
    return next((dict(item) for item in LESSONS if item["id"] not in completed), None)


def lesson_cache_key(
    lesson_id: str,
    experience: str,
    daily_minutes: int,
    notebook_signature: str,
) -> str:
    return ":".join((
        COURSE_VERSION,
        lesson_id,
        experience,
        str(daily_minutes),
        notebook_signature,
    ))


def get_cached_lesson(cache_key: str) -> dict | None:
    _ensure_initialized()
    with _db() as connection:
        row = connection.execute(
            "SELECT lesson_json FROM lesson_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["lesson_json"])
    except (TypeError, json.JSONDecodeError):
        return None


def save_cached_lesson(cache_key: str, lesson_id: str, lesson: dict) -> None:
    _ensure_initialized()
    with _db() as connection:
        connection.execute(
            """
            INSERT INTO lesson_cache(
                cache_key, lesson_id, course_version, lesson_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                lesson_json = excluded.lesson_json,
                created_at = excluded.created_at
            """,
            (
                cache_key,
                lesson_id,
                COURSE_VERSION,
                json.dumps(lesson, ensure_ascii=False),
                int(time.time()),
            ),
        )


def clear_lesson_cache() -> int:
    _ensure_initialized()
    with _db() as connection:
        cursor = connection.execute("DELETE FROM lesson_cache")
    return cursor.rowcount


def save_session(user_id: int, lesson_id: str, lesson: dict) -> dict:
    _ensure_initialized()
    now = int(time.time())
    payload = json.dumps(lesson, ensure_ascii=False)
    with _db() as connection:
        connection.execute(
            """
            INSERT INTO lesson_sessions(
                user_id, lesson_id, segment_index, state, lesson_json,
                score_total, answer_count, attempts, started_at, updated_at
            ) VALUES (?, ?, 0, 'awaiting_answer', ?, 0, 0, 0, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                lesson_id = excluded.lesson_id,
                segment_index = 0,
                state = 'awaiting_answer',
                lesson_json = excluded.lesson_json,
                score_total = 0,
                answer_count = 0,
                attempts = 0,
                started_at = excluded.started_at,
                updated_at = excluded.updated_at
            """,
            (user_id, lesson_id, payload, now, now),
        )
    return get_session(user_id) or {}


def get_session(user_id: int) -> dict | None:
    _ensure_initialized()
    with _db() as connection:
        row = connection.execute(
            "SELECT * FROM lesson_sessions WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    try:
        result["lesson"] = json.loads(result.pop("lesson_json"))
    except (TypeError, json.JSONDecodeError):
        result["lesson"] = {}
        result.pop("lesson_json", None)
    return result


def update_session(user_id: int, *, segment_index: int | None = None, state: str | None = None) -> None:
    _ensure_initialized()
    values = []
    assignments = []
    if segment_index is not None:
        assignments.append("segment_index = ?")
        values.append(segment_index)
    if state is not None:
        assignments.append("state = ?")
        values.append(state)
    if not assignments:
        return
    assignments.append("updated_at = ?")
    values.extend((int(time.time()), user_id))
    with _db() as connection:
        connection.execute(
            f"UPDATE lesson_sessions SET {', '.join(assignments)} WHERE user_id = ?",
            values,
        )


def record_score(user_id: int, score: int) -> dict | None:
    _ensure_initialized()
    bounded = max(0, min(3, int(score)))
    with _db() as connection:
        connection.execute(
            """
            UPDATE lesson_sessions
            SET score_total = score_total + ?,
                answer_count = answer_count + 1,
                attempts = attempts + 1,
                updated_at = ?
            WHERE user_id = ?
            """,
            (bounded, int(time.time()), user_id),
        )
    return get_session(user_id)


def record_retry(user_id: int) -> None:
    _ensure_initialized()
    with _db() as connection:
        connection.execute(
            """
            UPDATE lesson_sessions
            SET attempts = attempts + 1, updated_at = ?
            WHERE user_id = ?
            """,
            (int(time.time()), user_id),
        )


def pause_session(user_id: int) -> None:
    update_session(user_id, state="paused")


def complete_lesson(user_id: int) -> int:
    _ensure_initialized()
    session = get_session(user_id)
    if not session:
        return 0
    answers = max(1, int(session["answer_count"]))
    mastery = max(0, min(3, round(int(session["score_total"]) / answers)))
    now = int(time.time())
    review_delay = 86400 if mastery < 3 else 3 * 86400
    with _db() as connection:
        connection.execute(
            """
            INSERT INTO lesson_progress(
                user_id, lesson_id, status, mastery, attempts,
                completed_at, next_review_at, updated_at
            ) VALUES (?, ?, 'completed', ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, lesson_id) DO UPDATE SET
                status = 'completed',
                mastery = MAX(lesson_progress.mastery, excluded.mastery),
                attempts = lesson_progress.attempts + excluded.attempts,
                completed_at = excluded.completed_at,
                next_review_at = excluded.next_review_at,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                session["lesson_id"],
                mastery,
                int(session["attempts"]),
                now,
                now + review_delay,
                now,
            ),
        )
        connection.execute(
            "DELETE FROM lesson_sessions WHERE user_id = ?",
            (user_id,),
        )
    return mastery


def progress_summary(user_id: int) -> dict:
    _ensure_initialized()
    with _db() as connection:
        rows = connection.execute(
            "SELECT * FROM lesson_progress WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    completed = [dict(row) for row in rows if row["status"] == "completed"]
    average = (
        round(sum(int(row["mastery"]) for row in completed) / len(completed), 1)
        if completed
        else 0
    )
    return {
        "completed": len(completed),
        "total": len(LESSONS),
        "average_mastery": average,
    }


def due_review(user_id: int) -> dict | None:
    _ensure_initialized()
    now = int(time.time())
    with _db() as connection:
        row = connection.execute(
            """
            SELECT * FROM lesson_progress
            WHERE user_id = ? AND status = 'completed' AND next_review_at <= ?
            ORDER BY next_review_at ASC
            LIMIT 1
            """,
            (user_id, now),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["lesson"] = get_lesson(result["lesson_id"])
    return result


def latest_completed_lesson(user_id: int) -> dict | None:
    """Return the most recently completed lesson for an on-demand quiz."""
    _ensure_initialized()
    with _db() as connection:
        row = connection.execute(
            """
            SELECT * FROM lesson_progress
            WHERE user_id = ? AND status = 'completed'
            ORDER BY completed_at DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["lesson"] = get_lesson(result["lesson_id"])
    return result

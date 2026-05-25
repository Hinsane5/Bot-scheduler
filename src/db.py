"""SQLite persistence helpers."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src import config
from src.parser import Event


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "events.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT,
    location TEXT,
    link TEXT,
    notes TEXT,
    remind_before TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);

CREATE TABLE IF NOT EXISTS reminders_sent (
    event_id TEXT NOT NULL,
    lead_min INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (event_id, lead_min)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scraper TEXT NOT NULL,
    ran_at TEXT NOT NULL,
    success INTEGER NOT NULL,
    inserted INTEGER,
    updated INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS refresh_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portal TEXT NOT NULL,
    ran_at TEXT NOT NULL,
    success INTEGER NOT NULL,
    new_token_exp TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS llm_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at TEXT NOT NULL,
    user_message TEXT NOT NULL,
    intent_json TEXT,
    latency_ms INTEGER,
    error TEXT
);
"""


EVENT_COLUMNS = (
    "id",
    "source",
    "type",
    "title",
    "start",
    "end",
    "location",
    "link",
    "notes",
    "remind_before",
    "created_at",
    "updated_at",
)

EVENT_VALUE_COLUMNS = (
    "source",
    "type",
    "title",
    "start",
    "end",
    "location",
    "link",
    "notes",
    "remind_before",
)


def init(db_path: Path | str = DB_PATH) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as conn:
        conn.executescript(SCHEMA)


def upsert_events(events: list[Event], db_path: Path | str = DB_PATH) -> tuple[int, int]:
    """Insert or update events, returning ``(inserted, updated)``."""
    path = Path(db_path)
    init(path)
    inserted = 0
    updated = 0
    now = _now_iso()

    with _connect(path) as conn:
        for event in events:
            incoming = _event_values(event)
            existing = conn.execute(
                "SELECT * FROM events WHERE id = ?",
                (event.id,),
            ).fetchone()

            if existing is None:
                payload = {
                    **incoming,
                    "id": event.id,
                    "created_at": _datetime_to_text(event.created_at) or now,
                    "updated_at": _datetime_to_text(event.updated_at) or now,
                }
                conn.execute(
                    """
                    INSERT INTO events (
                        id, source, type, title, start, end, location, link,
                        notes, remind_before, created_at, updated_at
                    )
                    VALUES (
                        :id, :source, :type, :title, :start, :end, :location,
                        :link, :notes, :remind_before, :created_at, :updated_at
                    )
                    """,
                    payload,
                )
                inserted += 1
                continue

            if all(existing[column] == incoming[column] for column in EVENT_VALUE_COLUMNS):
                continue

            payload = {**incoming, "id": event.id, "updated_at": now}
            conn.execute(
                """
                UPDATE events
                SET source = :source,
                    type = :type,
                    title = :title,
                    start = :start,
                    end = :end,
                    location = :location,
                    link = :link,
                    notes = :notes,
                    remind_before = :remind_before,
                    updated_at = :updated_at
                WHERE id = :id
                """,
                payload,
            )
            updated += 1

    return inserted, updated


def list_events(
    start: datetime | date | None = None,
    end: datetime | date | None = None,
    *,
    type: str | None = None,
    source: str | None = None,
    limit: int | None = None,
    db_path: Path | str = DB_PATH,
) -> list[Event]:
    init(db_path)
    clauses: list[str] = []
    params: list[Any] = []

    if start is not None:
        clauses.append("start >= ?")
        params.append(_bound_to_text(start, is_end=False))
    if end is not None:
        clauses.append("start <= ?")
        params.append(_bound_to_text(end, is_end=True))
    if type is not None:
        clauses.append("type = ?")
        params.append(type)
    if source is not None:
        clauses.append("source = ?")
        params.append(source)

    query = f"SELECT {', '.join(EVENT_COLUMNS)} FROM events"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY start ASC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_event(row) for row in rows]


def get_event(event_id: str, db_path: Path | str = DB_PATH) -> Event | None:
    init(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {', '.join(EVENT_COLUMNS)} FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
    return _row_to_event(row) if row else None


def find_event_by_prefix(
    event_id_prefix: str,
    *,
    manual_only: bool = False,
    db_path: Path | str = DB_PATH,
) -> Event | None:
    init(db_path)
    prefix = event_id_prefix.strip()
    if not prefix:
        return None

    query = f"SELECT {', '.join(EVENT_COLUMNS)} FROM events WHERE id LIKE ?"
    params: list[Any] = [f"{prefix}%"]
    if manual_only:
        query += " AND source = ?"
        params.append("manual")
    query += " ORDER BY start ASC LIMIT 2"

    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    if len(rows) != 1:
        return None
    return _row_to_event(rows[0])


def delete_event(event_id: str, *, manual_only: bool = False, db_path: Path | str = DB_PATH) -> bool:
    init(db_path)
    query = "DELETE FROM events WHERE id = ?"
    params: tuple[Any, ...] = (event_id,)
    if manual_only:
        query += " AND source = ?"
        params = (event_id, "manual")
    with _connect(db_path) as conn:
        cursor = conn.execute(query, params)
        return cursor.rowcount > 0


def mark_reminded(event_id: str, lead_min: int, db_path: Path | str = DB_PATH) -> None:
    init(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO reminders_sent (event_id, lead_min, sent_at)
            VALUES (?, ?, ?)
            """,
            (event_id, lead_min, _now_iso()),
        )


def was_reminded(event_id: str, lead_min: int, db_path: Path | str = DB_PATH) -> bool:
    init(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM reminders_sent WHERE event_id = ? AND lead_min = ?",
            (event_id, lead_min),
        ).fetchone()
    return row is not None


def log_sync(
    scraper: str,
    success: bool,
    inserted: int | None = None,
    updated: int | None = None,
    error: str | None = None,
    db_path: Path | str = DB_PATH,
) -> None:
    init(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO sync_log (scraper, ran_at, success, inserted, updated, error)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (scraper, _now_iso(), int(success), inserted, updated, error),
        )


def last_sync(scraper: str | None = None, db_path: Path | str = DB_PATH) -> dict[str, Any] | None:
    init(db_path)
    query = "SELECT * FROM sync_log"
    params: tuple[Any, ...] = ()
    if scraper is not None:
        query += " WHERE scraper = ?"
        params = (scraper,)
    query += " ORDER BY ran_at DESC LIMIT 1"
    with _connect(db_path) as conn:
        row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def log_refresh(
    portal: str,
    success: bool,
    new_token_exp: str | None = None,
    error: str | None = None,
    db_path: Path | str = DB_PATH,
) -> None:
    init(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO refresh_log (portal, ran_at, success, new_token_exp, error)
            VALUES (?, ?, ?, ?, ?)
            """,
            (portal, _now_iso(), int(success), new_token_exp, error),
        )


def last_refresh(portal: str | None = None, db_path: Path | str = DB_PATH) -> dict[str, Any] | None:
    init(db_path)
    query = "SELECT * FROM refresh_log"
    params: tuple[Any, ...] = ()
    if portal is not None:
        query += " WHERE portal = ?"
        params = (portal,)
    query += " ORDER BY ran_at DESC LIMIT 1"
    with _connect(db_path) as conn:
        row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def log_llm(
    user_message: str,
    intent: dict[str, Any] | None = None,
    latency_ms: int | None = None,
    error: str | None = None,
    db_path: Path | str = DB_PATH,
) -> None:
    init(db_path)
    intent_json = json.dumps(intent, sort_keys=True) if intent is not None else None
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO llm_log (ran_at, user_message, intent_json, latency_ms, error)
            VALUES (?, ?, ?, ?, ?)
            """,
            (_now_iso(), user_message, intent_json, latency_ms, error),
        )


def prune_stale(source: str, older_than: datetime, db_path: Path | str = DB_PATH) -> int:
    init(db_path)
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM events WHERE source = ? AND updated_at < ?",
            (source, _datetime_to_text(older_than)),
        )
        return cursor.rowcount


def backup_to(path: Path | str, db_path: Path | str = DB_PATH) -> None:
    init(db_path)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as src_conn:
        with sqlite3.connect(destination) as dest_conn:
            src_conn.backup(dest_conn)


def _connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _event_values(event: Event) -> dict[str, Any]:
    return {
        "source": event.source,
        "type": event.type,
        "title": event.title,
        "start": _datetime_to_text(event.start),
        "end": _datetime_to_text(event.end),
        "location": event.location,
        "link": event.link,
        "notes": event.notes,
        "remind_before": json.dumps(event.remind_before),
    }


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        source=row["source"],
        type=row["type"],
        title=row["title"],
        start=_text_to_datetime(row["start"]),
        end=_text_to_datetime(row["end"]) if row["end"] else None,
        location=row["location"],
        link=row["link"],
        notes=row["notes"],
        remind_before=json.loads(row["remind_before"]),
        created_at=_text_to_datetime(row["created_at"]),
        updated_at=_text_to_datetime(row["updated_at"]),
    )


def _datetime_to_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=config.TZ)
    return value.isoformat()


def _text_to_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=config.TZ)
    return parsed


def _bound_to_text(value: datetime | date, *, is_end: bool) -> str:
    if isinstance(value, datetime):
        return _datetime_to_text(value) or ""
    time_text = "23:59:59.999999" if is_end else "00:00:00"
    return f"{value.isoformat()}T{time_text}+07:00"


def _now_iso() -> str:
    return datetime.now(tz=config.TZ).isoformat()

"""
Lightweight SQLite persistence for book history.

Uses a single `books` table. Thread-safe via sqlite3's check_same_thread=False
plus a module-level lock for writes.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path(__file__).resolve().parent / "bookwriter.db"
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    with _lock:
        conn = _connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS books (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                agent TEXT NOT NULL DEFAULT 'main',
                model TEXT NOT NULL DEFAULT '',
                input_docx TEXT NOT NULL DEFAULT '',
                final_docx TEXT NOT NULL DEFAULT '',
                kindle_docx TEXT NOT NULL DEFAULT '',
                paperback_docx TEXT NOT NULL DEFAULT '',
                headings TEXT NOT NULL DEFAULT '[]',
                listing TEXT NOT NULL DEFAULT '{}',
                config TEXT NOT NULL DEFAULT '{}',
                logs TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '',
                pre_written INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.commit()
        # Migrate: add missing columns for existing DBs
        cols = {row[1] for row in conn.execute("PRAGMA table_info(books)").fetchall()}
        if "logs" not in cols:
            conn.execute("ALTER TABLE books ADD COLUMN logs TEXT NOT NULL DEFAULT '[]'")
            conn.commit()
        if "pre_written" not in cols:
            conn.execute("ALTER TABLE books ADD COLUMN pre_written INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        conn.close()


def save_book(
    book_id: str,
    *,
    title: str = "",
    status: str = "queued",
    agent: str = "main",
    model: str = "",
    input_docx: str = "",
    final_docx: str = "",
    kindle_docx: str = "",
    paperback_docx: str = "",
    headings: list[str] | None = None,
    listing: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    logs: list[str] | None = None,
    error: str = "",
    pre_written: bool = False,
) -> None:
    """Insert or update a book record."""
    now = time.time()
    headings_json = json.dumps(headings or [])
    listing_json = json.dumps(listing or {})
    config_json = json.dumps(config or {})
    logs_json = json.dumps(logs or [])
    pre_written_int = 1 if pre_written else 0

    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO books (id, title, status, agent, model,
                               input_docx, final_docx, kindle_docx, paperback_docx,
                               headings, listing, config, logs, error, pre_written,
                               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                status=excluded.status,
                agent=excluded.agent,
                model=excluded.model,
                input_docx=excluded.input_docx,
                final_docx=excluded.final_docx,
                kindle_docx=excluded.kindle_docx,
                paperback_docx=excluded.paperback_docx,
                headings=excluded.headings,
                listing=excluded.listing,
                config=excluded.config,
                logs=excluded.logs,
                error=excluded.error,
                pre_written=excluded.pre_written,
                updated_at=excluded.updated_at
            """,
            (
                book_id, title, status, agent, model,
                input_docx, final_docx, kindle_docx, paperback_docx,
                headings_json, listing_json, config_json, logs_json, error, pre_written_int,
                now, now,
            ),
        )
        conn.commit()
        conn.close()


def update_book(book_id: str, **fields: Any) -> None:
    """Update specific fields of a book."""
    allowed = {
        "title", "status", "agent", "model",
        "input_docx", "final_docx", "kindle_docx", "paperback_docx",
        "headings", "listing", "config", "logs", "error",
    }
    updates: dict[str, Any] = {}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("headings", "listing", "config", "logs"):
            updates[k] = json.dumps(v)
        else:
            updates[k] = v

    if not updates:
        return

    updates["updated_at"] = time.time()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [book_id]

    with _lock:
        conn = _connect()
        conn.execute(f"UPDATE books SET {set_clause} WHERE id=?", values)
        conn.commit()
        conn.close()


def list_books(limit: int = 50) -> list[dict[str, Any]]:
    """Return recent books, newest first."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, title, status, agent, model, pre_written, created_at, updated_at "
        "FROM books ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_book(book_id: str) -> Optional[dict[str, Any]]:
    """Return full book record or None."""
    conn = _connect()
    row = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    d = dict(row)
    d["headings"] = json.loads(d["headings"])
    d["listing"] = json.loads(d["listing"])
    d["config"] = json.loads(d["config"])
    d["logs"] = json.loads(d.get("logs") or "[]")
    return d


def delete_book(book_id: str) -> bool:
    """Delete a book record. Returns True if deleted."""
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM books WHERE id=?", (book_id,))
        conn.commit()
        conn.close()
        return cur.rowcount > 0

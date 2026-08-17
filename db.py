"""
Lightweight SQLite persistence for book history.

Uses a single `books` table. Thread-safe via sqlite3's check_same_thread=False
plus a module-level lock for writes.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
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
    # Ensure .env is loaded before we seed env-backed rows (Amazon Ads account).
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except Exception:
        pass
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

        # Publications — books that are live (or being prepared to go live)
        # on Amazon. One row covers a book worldwide; per-marketplace ASINs
        # live inside the marketplaces JSON column.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS publications (
                id TEXT PRIMARY KEY,
                book_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                subtitle TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                categories TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'draft',
                marketplaces TEXT NOT NULL DEFAULT '{}',
                primary_asin TEXT NOT NULL DEFAULT '',
                primary_marketplace TEXT NOT NULL DEFAULT '',
                kindle_docx_path TEXT NOT NULL DEFAULT '',
                paperback_docx_path TEXT NOT NULL DEFAULT '',
                front_cover_path TEXT NOT NULL DEFAULT '',
                source_zip_path TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_publications_status
            ON publications(status, updated_at DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_publications_book
            ON publications(book_id)
        """)
        conn.commit()

        # Migration: ensure amazon_account_id column exists on publications.
        # Retained as the publisher/company key that scopes MailerLite config.
        pub_cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(publications)").fetchall()}
        if "amazon_account_id" not in pub_cols:
            conn.execute(
                "ALTER TABLE publications "
                "ADD COLUMN amazon_account_id TEXT NOT NULL DEFAULT ''"
            )
        conn.commit()

        # Review-request recipients per publication.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_recipients (
                id TEXT PRIMARY KEY,
                publication_id TEXT NOT NULL,
                email TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                marketplace TEXT NOT NULL DEFAULT 'US',
                trigger_date REAL NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                unsubscribe_token TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(publication_id, email)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_review_recipients_pub
            ON review_recipients(publication_id, status)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_review_recipients_unsub
            ON review_recipients(unsubscribe_token)
        """)

        # Individual scheduled / sent emails. One row per recipient per step.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_email_sends (
                id TEXT PRIMARY KEY,
                publication_id TEXT NOT NULL,
                recipient_id TEXT NOT NULL,
                step INTEGER NOT NULL,
                scheduled_for REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'scheduled',
                subject TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                sent_at REAL,
                send_method TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                UNIQUE(recipient_id, step)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_email_sends_due
            ON review_email_sends(status, scheduled_for)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_email_sends_pub
            ON review_email_sends(publication_id, status)
        """)

        # Per-publication review automation settings (templates, schedule, etc).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_settings (
                publication_id TEXT PRIMARY KEY,
                launch_date REAL,
                from_name TEXT NOT NULL DEFAULT '',
                from_email TEXT NOT NULL DEFAULT '',
                schedule_days TEXT NOT NULL DEFAULT '[7,14,30]',
                templates TEXT NOT NULL DEFAULT '[]',
                updated_at REAL NOT NULL
            )
        """)
        conn.commit()

        # Launch-email campaigns: one row per publication, holds 3-5 email drafts.
        # Scoped to publication_id (not book_id) so each Amazon listing can have
        # its own promo sequence pushed to MailerLite as drafts.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_campaigns (
                publication_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'draft',
                format TEXT NOT NULL DEFAULT 'markdown',
                sequence_length INTEGER NOT NULL DEFAULT 5,
                book_snapshot TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                launch_date TEXT NOT NULL DEFAULT '',
                launch_time TEXT NOT NULL DEFAULT '',
                launch_timezone TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(publication_id) REFERENCES publications(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                publication_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                day_offset INTEGER NOT NULL DEFAULT 0,
                subject TEXT NOT NULL DEFAULT '',
                preview TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                cta_label TEXT NOT NULL DEFAULT '',
                cta_url TEXT NOT NULL DEFAULT '',
                pushed_to TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(publication_id, position),
                FOREIGN KEY(publication_id) REFERENCES publications(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_emails_pub
            ON emails(publication_id, position)
        """)

        # Generic key/value settings — used by MailerLite (mailerlite.api_key,
        # mailerlite.from_email, mailerlite.from_name, mailerlite.default_group_id)
        # and any other org-level integration we wire up later.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            )
        """)
        conn.commit()

        # QR code library — saved QR codes that can be attached to any book.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS qr_codes (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                style TEXT NOT NULL DEFAULT 'square',
                fg_color TEXT NOT NULL DEFAULT '#111827',
                bg_color TEXT NOT NULL DEFAULT '#ffffff',
                error_correction TEXT NOT NULL DEFAULT 'M',
                box_size INTEGER NOT NULL DEFAULT 12,
                border INTEGER NOT NULL DEFAULT 4,
                png_blob BLOB NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.commit()

        # Trivia & facts books. Separate from `books` on purpose: a trivia book
        # is structured content (questions, choices, facts, answer key) rather
        # than a prose manuscript, and the two pipelines share no fields beyond
        # id/title/status.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trivia_books (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                topic TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                stage TEXT NOT NULL DEFAULT '',
                progress REAL NOT NULL DEFAULT 0,
                agent TEXT NOT NULL DEFAULT 'main',
                difficulty TEXT NOT NULL DEFAULT 'medium',
                answer_key_position TEXT NOT NULL DEFAULT 'end_of_book',
                chapter_count INTEGER NOT NULL DEFAULT 0,
                trivia_total INTEGER NOT NULL DEFAULT 0,
                fact_total INTEGER NOT NULL DEFAULT 0,
                config_json TEXT NOT NULL DEFAULT '',
                json_path TEXT NOT NULL DEFAULT '',
                markdown_path TEXT NOT NULL DEFAULT '',
                docx_path TEXT NOT NULL DEFAULT '',
                kindle_path TEXT NOT NULL DEFAULT '',
                paperback_path TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                warnings_json TEXT NOT NULL DEFAULT '',
                usage_json TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.commit()
        # Migrate: add usage tracking to existing trivia_books tables.
        tcols = {r[1] for r in conn.execute("PRAGMA table_info(trivia_books)").fetchall()}
        if "usage_json" not in tcols:
            conn.execute("ALTER TABLE trivia_books ADD COLUMN usage_json TEXT NOT NULL DEFAULT ''")
            conn.commit()

        # Puzzle & activity books. Separate from both `books` and
        # `trivia_books`: a puzzle book's outputs are largely rendered artwork
        # (mazes, grids) plus a handoff zip for the formatter, which neither of
        # the other two pipelines produces.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS puzzle_books (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                topic TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                stage TEXT NOT NULL DEFAULT '',
                progress REAL NOT NULL DEFAULT 0,
                agent TEXT NOT NULL DEFAULT 'main',
                audience TEXT NOT NULL DEFAULT '',
                difficulty TEXT NOT NULL DEFAULT 'medium',
                counts_json TEXT NOT NULL DEFAULT '',
                estimated_pages INTEGER NOT NULL DEFAULT 0,
                config_json TEXT NOT NULL DEFAULT '',
                json_path TEXT NOT NULL DEFAULT '',
                markdown_path TEXT NOT NULL DEFAULT '',
                docx_path TEXT NOT NULL DEFAULT '',
                kindle_path TEXT NOT NULL DEFAULT '',
                paperback_path TEXT NOT NULL DEFAULT '',
                zip_path TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                warnings_json TEXT NOT NULL DEFAULT '',
                usage_json TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.commit()

        # Researched-stories books. Own table again: the unit here is a story
        # with a free-form context box and a fact-check trail, which none of the
        # other three pipelines carry.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS story_books (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                topic TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                stage TEXT NOT NULL DEFAULT '',
                progress REAL NOT NULL DEFAULT 0,
                agent TEXT NOT NULL DEFAULT 'main',
                audience TEXT NOT NULL DEFAULT '',
                tone TEXT NOT NULL DEFAULT '',
                story_count INTEGER NOT NULL DEFAULT 0,
                stories_written INTEGER NOT NULL DEFAULT 0,
                total_words INTEGER NOT NULL DEFAULT 0,
                min_words INTEGER NOT NULL DEFAULT 300,
                max_words INTEGER NOT NULL DEFAULT 500,
                config_json TEXT NOT NULL DEFAULT '',
                json_path TEXT NOT NULL DEFAULT '',
                markdown_path TEXT NOT NULL DEFAULT '',
                docx_path TEXT NOT NULL DEFAULT '',
                kindle_path TEXT NOT NULL DEFAULT '',
                paperback_path TEXT NOT NULL DEFAULT '',
                factcheck_path TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                warnings_json TEXT NOT NULL DEFAULT '',
                usage_json TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
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


def list_books(limit: int = 50, dashboard_mode: str | None = None) -> list[dict[str, Any]]:
    """Return recent books, newest first, optionally filtered by dashboard mode."""
    dashboard_mode = (dashboard_mode or "").strip().lower() or None
    try:
        limit_n = int(limit)
    except Exception:
        limit_n = 50
    if limit_n <= 0:
        return []

    conn = _connect()
    rows = conn.execute(
        "SELECT id, title, status, agent, model, pre_written, created_at, updated_at, config "
        "FROM books ORDER BY updated_at DESC",
    ).fetchall()
    conn.close()

    out: list[dict[str, Any]] = []
    for row in rows:
        rec = dict(row)
        cfg_raw = rec.pop("config", "{}")
        rec_mode = "standard"
        try:
            cfg = json.loads(cfg_raw or "{}")
            if isinstance(cfg, dict):
                mode = str(cfg.get("dashboard_mode") or "").strip().lower()
                if mode == "long-book":
                    rec_mode = "long-book"
        except Exception:
            pass

        if dashboard_mode and rec_mode != dashboard_mode:
            continue

        rec["dashboard_mode"] = rec_mode
        out.append(rec)
        if len(out) >= limit_n:
            break

    return out


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


# ----- QR codes -----

def save_qr_code(
    qr_id: str,
    *,
    label: str,
    content: str,
    style: str,
    fg_color: str,
    bg_color: str,
    error_correction: str,
    box_size: int,
    border: int,
    png_blob: bytes,
) -> None:
    """Insert a saved QR code into the library."""
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO qr_codes
                (id, label, content, style, fg_color, bg_color, error_correction,
                 box_size, border, png_blob, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                qr_id, label, content, style, fg_color, bg_color, error_correction,
                int(box_size), int(border), png_blob, time.time(),
            ),
        )
        conn.commit()
        conn.close()


def list_qr_codes(limit: int = 200) -> list[dict[str, Any]]:
    """Return saved QR codes, newest first. Excludes the BLOB."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, label, content, style, fg_color, bg_color, error_correction, "
        "box_size, border, created_at FROM qr_codes ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_qr_code(qr_id: str) -> Optional[dict[str, Any]]:
    """Return full QR record (including png_blob) or None."""
    conn = _connect()
    row = conn.execute("SELECT * FROM qr_codes WHERE id=?", (qr_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def delete_qr_code(qr_id: str) -> bool:
    """Delete a QR code. Returns True if removed."""
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM qr_codes WHERE id=?", (qr_id,))
        conn.commit()
        conn.close()
        return cur.rowcount > 0

# ----- Publications -----

# Marketplaces we present as primary citizens. Other marketplaces still work
# but the UI focuses on these four.
PRIMARY_MARKETPLACES = ("US", "UK", "CA", "AU")


def _empty_marketplaces() -> dict[str, Any]:
    return {mp: None for mp in PRIMARY_MARKETPLACES}


def _coerce_marketplaces(value: Any) -> dict[str, Any]:
    """Normalize a marketplaces dict so every primary marketplace key exists."""
    out = _empty_marketplaces()
    if isinstance(value, dict):
        for k, v in value.items():
            key = (k or "").upper()
            if v in (None, ""):
                out[key] = None
            elif isinstance(v, dict):
                # Only keep the canonical fields we care about
                clean = {
                    "asin": str(v.get("asin", "")).strip(),
                    "url": str(v.get("url", "")).strip(),
                    "live_at": v.get("live_at") or None,
                    "notes": str(v.get("notes", "")).strip(),
                }
                if clean["asin"]:
                    out[key] = clean
                else:
                    out[key] = None
            else:
                out[key] = None
    return out


def _derive_primary(marketplaces: dict[str, Any]) -> tuple[str, str]:
    """Return (primary_marketplace, primary_asin) preferring US > UK > CA > AU."""
    for mp in PRIMARY_MARKETPLACES:
        info = marketplaces.get(mp)
        if info and info.get("asin"):
            return mp, info["asin"]
    # Fall back to any other marketplace with an ASIN.
    for mp, info in marketplaces.items():
        if info and info.get("asin"):
            return mp, info["asin"]
    return "", ""


def save_publication(
    pub_id: str,
    *,
    book_id: str = "",
    title: str = "",
    subtitle: str = "",
    description: str = "",
    categories: list[Any] | None = None,
    status: str = "draft",
    marketplaces: dict[str, Any] | None = None,
    kindle_docx_path: str = "",
    paperback_docx_path: str = "",
    front_cover_path: str = "",
    source_zip_path: str = "",
    notes: str = "",
    amazon_account_id: str = "",
) -> None:
    """Insert or update a publication."""
    now = time.time()
    mp_norm = _coerce_marketplaces(marketplaces or {})
    primary_mp, primary_asin = _derive_primary(mp_norm)
    # Auto-promote draft -> live once at least one ASIN is filled in.
    effective_status = status
    if primary_asin and status in ("", "draft"):
        effective_status = "live"

    cats_json = json.dumps(categories or [])
    mp_json = json.dumps(mp_norm)

    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO publications (
                id, book_id, title, subtitle, description, categories,
                status, marketplaces, primary_asin, primary_marketplace,
                kindle_docx_path, paperback_docx_path, front_cover_path,
                source_zip_path, notes, created_at, updated_at,
                amazon_account_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                book_id=excluded.book_id,
                title=excluded.title,
                subtitle=excluded.subtitle,
                description=excluded.description,
                categories=excluded.categories,
                status=excluded.status,
                marketplaces=excluded.marketplaces,
                primary_asin=excluded.primary_asin,
                primary_marketplace=excluded.primary_marketplace,
                kindle_docx_path=excluded.kindle_docx_path,
                paperback_docx_path=excluded.paperback_docx_path,
                front_cover_path=excluded.front_cover_path,
                source_zip_path=excluded.source_zip_path,
                notes=excluded.notes,
                updated_at=excluded.updated_at,
                amazon_account_id=excluded.amazon_account_id
            """,
            (
                pub_id, book_id, title, subtitle, description, cats_json,
                effective_status, mp_json, primary_asin, primary_mp,
                kindle_docx_path, paperback_docx_path, front_cover_path,
                source_zip_path, notes, now, now,
                amazon_account_id,
            ),
        )
        conn.commit()
        conn.close()


def update_publication(pub_id: str, **fields: Any) -> bool:
    """Partial update. Returns True if updated."""
    current = get_publication(pub_id)
    if not current:
        return False
    merged = dict(current)
    # Merge top-level fields
    for k, v in fields.items():
        if k == "marketplaces" and isinstance(v, dict):
            # shallow-merge incoming marketplace entries over existing ones
            new_mp = dict(current.get("marketplaces") or {})
            for mp, info in v.items():
                new_mp[(mp or "").upper()] = info
            merged["marketplaces"] = new_mp
        else:
            merged[k] = v
    save_publication(
        pub_id,
        book_id=merged.get("book_id", "") or "",
        title=merged.get("title", "") or "",
        subtitle=merged.get("subtitle", "") or "",
        description=merged.get("description", "") or "",
        categories=merged.get("categories") or [],
        status=merged.get("status", "draft") or "draft",
        marketplaces=merged.get("marketplaces") or {},
        kindle_docx_path=merged.get("kindle_docx_path", "") or "",
        paperback_docx_path=merged.get("paperback_docx_path", "") or "",
        front_cover_path=merged.get("front_cover_path", "") or "",
        source_zip_path=merged.get("source_zip_path", "") or "",
        notes=merged.get("notes", "") or "",
        amazon_account_id=merged.get("amazon_account_id", "") or "",
    )
    return True


def list_publications(limit: int = 200) -> list[dict[str, Any]]:
    """Return publications, newest first. JSON columns are decoded."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM publications ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["categories"] = json.loads(d.get("categories") or "[]")
        except Exception:
            d["categories"] = []
        try:
            d["marketplaces"] = _coerce_marketplaces(
                json.loads(d.get("marketplaces") or "{}")
            )
        except Exception:
            d["marketplaces"] = _empty_marketplaces()
        out.append(d)
    return out


def get_publication(pub_id: str) -> Optional[dict[str, Any]]:
    conn = _connect()
    row = conn.execute("SELECT * FROM publications WHERE id=?", (pub_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    d = dict(row)
    try:
        d["categories"] = json.loads(d.get("categories") or "[]")
    except Exception:
        d["categories"] = []
    try:
        d["marketplaces"] = _coerce_marketplaces(
            json.loads(d.get("marketplaces") or "{}")
        )
    except Exception:
        d["marketplaces"] = _empty_marketplaces()
    return d


def find_publication_by_book(book_id: str) -> Optional[dict[str, Any]]:
    """Find the (most recent) publication linked to a book, if any."""
    if not book_id:
        return None
    conn = _connect()
    row = conn.execute(
        "SELECT id FROM publications WHERE book_id=? "
        "ORDER BY updated_at DESC LIMIT 1",
        (book_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return get_publication(row[0])


def delete_publication(pub_id: str) -> bool:
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM publications WHERE id=?", (pub_id,))
        conn.commit()
        conn.close()
        return cur.rowcount > 0


# ----- Review request automation -----

def _new_id(prefix: str = "") -> str:
    import uuid as _uuid
    return (prefix + _uuid.uuid4().hex[:12])


# --- Settings -----------------------------------------------------------

def get_review_settings(pub_id: str) -> dict[str, Any]:
    """Return per-publication review automation settings (with defaults)."""
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM review_settings WHERE publication_id=?", (pub_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return {
            "publication_id": pub_id,
            "launch_date": None,
            "from_name": "",
            "from_email": "",
            "schedule_days": [7, 14, 30],
            "templates": [],
            "updated_at": None,
        }
    d = dict(row)
    try:
        d["schedule_days"] = json.loads(d.get("schedule_days") or "[7,14,30]")
    except Exception:
        d["schedule_days"] = [7, 14, 30]
    try:
        d["templates"] = json.loads(d.get("templates") or "[]")
    except Exception:
        d["templates"] = []
    return d


def save_review_settings(
    pub_id: str,
    *,
    launch_date: float | None = None,
    from_name: str = "",
    from_email: str = "",
    schedule_days: list[int] | None = None,
    templates: list[dict[str, Any]] | None = None,
) -> None:
    now = time.time()
    days = schedule_days if schedule_days is not None else [7, 14, 30]
    tpls = templates if templates is not None else []
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO review_settings
                (publication_id, launch_date, from_name, from_email,
                 schedule_days, templates, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(publication_id) DO UPDATE SET
                launch_date=excluded.launch_date,
                from_name=excluded.from_name,
                from_email=excluded.from_email,
                schedule_days=excluded.schedule_days,
                templates=excluded.templates,
                updated_at=excluded.updated_at
            """,
            (
                pub_id, launch_date, from_name, from_email,
                json.dumps(days), json.dumps(tpls), now,
            ),
        )
        conn.commit()
        conn.close()


# --- Recipients ---------------------------------------------------------

def add_review_recipient(
    publication_id: str,
    *,
    email: str,
    name: str = "",
    marketplace: str = "US",
    trigger_date: float | None = None,
    source: str = "manual",
    notes: str = "",
) -> tuple[str, bool]:
    """Insert a recipient. Returns (recipient_id, created).

    If a recipient with the same (publication_id, email) already exists,
    returns its id and created=False without touching it.
    """
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email is required")
    now = time.time()
    trig = trigger_date if trigger_date is not None else now
    rid = _new_id("r")
    token = _new_id("u") + _new_id()
    with _lock:
        conn = _connect()
        existing = conn.execute(
            "SELECT id FROM review_recipients WHERE publication_id=? AND email=?",
            (publication_id, email),
        ).fetchone()
        if existing is not None:
            conn.close()
            return existing[0], False
        conn.execute(
            """
            INSERT INTO review_recipients
              (id, publication_id, email, name, marketplace, trigger_date,
               source, status, unsubscribe_token, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (rid, publication_id, email, name, marketplace.upper(), trig,
             source, token, notes, now, now),
        )
        conn.commit()
        conn.close()
    return rid, True


def list_review_recipients(
    publication_id: str, *, status: str | None = None, limit: int = 1000
) -> list[dict[str, Any]]:
    conn = _connect()
    if status:
        rows = conn.execute(
            "SELECT * FROM review_recipients WHERE publication_id=? AND status=? "
            "ORDER BY created_at DESC LIMIT ?",
            (publication_id, status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM review_recipients WHERE publication_id=? "
            "ORDER BY created_at DESC LIMIT ?",
            (publication_id, limit),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_review_recipient(recipient_id: str) -> Optional[dict[str, Any]]:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM review_recipients WHERE id=?", (recipient_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def find_recipient_by_unsubscribe_token(token: str) -> Optional[dict[str, Any]]:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM review_recipients WHERE unsubscribe_token=?", (token,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def set_review_recipient_status(recipient_id: str, status: str) -> bool:
    """Set recipient status. Allowed: pending, reviewed, unsubscribed."""
    if status not in ("pending", "reviewed", "unsubscribed"):
        raise ValueError(f"bad status {status!r}")
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "UPDATE review_recipients SET status=?, updated_at=? WHERE id=?",
            (status, time.time(), recipient_id),
        )
        # Cancel any scheduled (not-yet-sent) emails for this recipient.
        if status in ("reviewed", "unsubscribed"):
            conn.execute(
                "UPDATE review_email_sends SET status='cancelled' "
                "WHERE recipient_id=? AND status='scheduled'",
                (recipient_id,),
            )
        conn.commit()
        conn.close()
        return cur.rowcount > 0


def delete_review_recipient(recipient_id: str) -> bool:
    with _lock:
        conn = _connect()
        conn.execute(
            "DELETE FROM review_email_sends WHERE recipient_id=?",
            (recipient_id,),
        )
        cur = conn.execute(
            "DELETE FROM review_recipients WHERE id=?", (recipient_id,)
        )
        conn.commit()
        conn.close()
        return cur.rowcount > 0


# --- Email sends --------------------------------------------------------

def schedule_email_send(
    *,
    publication_id: str,
    recipient_id: str,
    step: int,
    scheduled_for: float,
    subject: str,
    body: str,
) -> str:
    """Create a scheduled email send. Idempotent on (recipient_id, step)."""
    sid = _new_id("s")
    now = time.time()
    with _lock:
        conn = _connect()
        existing = conn.execute(
            "SELECT id FROM review_email_sends WHERE recipient_id=? AND step=?",
            (recipient_id, step),
        ).fetchone()
        if existing is not None:
            conn.close()
            return existing[0]
        conn.execute(
            """
            INSERT INTO review_email_sends
              (id, publication_id, recipient_id, step, scheduled_for,
               status, subject, body, created_at)
            VALUES (?, ?, ?, ?, ?, 'scheduled', ?, ?, ?)
            """,
            (sid, publication_id, recipient_id, step, scheduled_for,
             subject, body, now),
        )
        conn.commit()
        conn.close()
    return sid


def list_email_sends(
    publication_id: str | None = None,
    *,
    status: str | None = None,
    due_before: float | None = None,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM review_email_sends WHERE 1=1"
    params: list[Any] = []
    if publication_id:
        sql += " AND publication_id=?"
        params.append(publication_id)
    if status:
        sql += " AND status=?"
        params.append(status)
    if due_before is not None:
        sql += " AND scheduled_for <= ?"
        params.append(due_before)
    sql += " ORDER BY scheduled_for ASC LIMIT ?"
    params.append(limit)
    conn = _connect()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_email_send(
    send_id: str,
    *,
    status: str,
    method: str = "",
    error: str = "",
) -> bool:
    """Update an email-send row to sent / failed / cancelled."""
    if status not in ("sent", "failed", "cancelled", "scheduled"):
        raise ValueError(f"bad status {status!r}")
    now = time.time()
    with _lock:
        conn = _connect()
        cur = conn.execute(
            """
            UPDATE review_email_sends
            SET status=?, send_method=?, sent_at=?, error=?
            WHERE id=?
            """,
            (status, method, now if status == "sent" else None, error, send_id),
        )
        conn.commit()
        conn.close()
        return cur.rowcount > 0


def get_email_send(send_id: str) -> Optional[dict[str, Any]]:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM review_email_sends WHERE id=?", (send_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def review_summary(publication_id: str) -> dict[str, int]:
    """Quick counts for the UI badge."""
    conn = _connect()
    out = {
        "recipients_total": 0,
        "recipients_pending": 0,
        "recipients_reviewed": 0,
        "recipients_unsubscribed": 0,
        "sends_scheduled": 0,
        "sends_due": 0,
        "sends_sent": 0,
        "sends_failed": 0,
    }
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM review_recipients WHERE publication_id=? "
        "GROUP BY status",
        (publication_id,),
    ).fetchall()
    for s, n in rows:
        out["recipients_total"] += n
        if s == "pending": out["recipients_pending"] = n
        elif s == "reviewed": out["recipients_reviewed"] = n
        elif s == "unsubscribed": out["recipients_unsubscribed"] = n
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM review_email_sends WHERE publication_id=? "
        "GROUP BY status",
        (publication_id,),
    ).fetchall()
    for s, n in rows:
        if s == "scheduled": out["sends_scheduled"] = n
        elif s == "sent": out["sends_sent"] = n
        elif s == "failed": out["sends_failed"] = n
    due = conn.execute(
        "SELECT COUNT(*) FROM review_email_sends "
        "WHERE publication_id=? AND status='scheduled' AND scheduled_for <= ?",
        (publication_id, time.time()),
    ).fetchone()[0]
    out["sends_due"] = int(due or 0)
    conn.close()
    return out


# ---------------------------------------------------------------------------
# Launch-email campaigns (one row per publication, with N email drafts).
# Pushed to MailerLite as drafts — NEVER auto-sent by this server.
# ---------------------------------------------------------------------------


def save_email_campaign(
    publication_id: str,
    *,
    emails: list[dict[str, Any]],
    status: str = "draft",
    format: str = "markdown",
    book_snapshot: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    """Upsert a campaign + replace its email rows atomically."""
    now = time.time()
    snapshot_json = json.dumps(book_snapshot or {})
    seq_len = len(emails)
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO email_campaigns
                (publication_id, status, format, sequence_length,
                 book_snapshot, error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(publication_id) DO UPDATE SET
                status=excluded.status,
                format=excluded.format,
                sequence_length=excluded.sequence_length,
                book_snapshot=excluded.book_snapshot,
                error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (publication_id, status, format, seq_len, snapshot_json,
             error, now, now),
        )
        conn.execute("DELETE FROM emails WHERE publication_id=?", (publication_id,))
        for em in emails:
            conn.execute(
                """
                INSERT INTO emails
                    (publication_id, position, day_offset, subject, preview,
                     body, cta_label, cta_url, pushed_to, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    publication_id,
                    int(em.get("position", 0)),
                    int(em.get("day_offset", 0)),
                    str(em.get("subject", "")),
                    str(em.get("preview", "")),
                    str(em.get("body", "")),
                    str(em.get("cta_label", "")),
                    str(em.get("cta_url", "")),
                    str(em.get("pushed_to", "")),
                    now, now,
                ),
            )
        conn.commit()
        conn.close()


def update_email_campaign(publication_id: str, **fields: Any) -> None:
    """Update campaign-level fields (status / error / format / launch schedule)."""
    allowed = {"status", "format", "error",
               "launch_date", "launch_time", "launch_timezone"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    updates["updated_at"] = time.time()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [publication_id]
    with _lock:
        conn = _connect()
        conn.execute(
            f"UPDATE email_campaigns SET {set_clause} WHERE publication_id=?",
            values,
        )
        conn.commit()
        conn.close()


def update_email(publication_id: str, position: int, **fields: Any) -> bool:
    """Update fields of one email draft. Returns True if a row changed."""
    allowed = {"day_offset", "subject", "preview", "body",
               "cta_label", "cta_url", "pushed_to"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    updates["updated_at"] = time.time()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [publication_id, position]
    with _lock:
        conn = _connect()
        cur = conn.execute(
            f"UPDATE emails SET {set_clause} "
            f"WHERE publication_id=? AND position=?",
            values,
        )
        conn.commit()
        conn.close()
        return cur.rowcount > 0


def get_email_campaign(publication_id: str) -> Optional[dict[str, Any]]:
    """Return the campaign + its emails, or None."""
    conn = _connect()
    camp = conn.execute(
        "SELECT * FROM email_campaigns WHERE publication_id=?",
        (publication_id,),
    ).fetchone()
    if camp is None:
        conn.close()
        return None
    rows = conn.execute(
        "SELECT * FROM emails WHERE publication_id=? ORDER BY position ASC",
        (publication_id,),
    ).fetchall()
    conn.close()
    out = dict(camp)
    try:
        out["book_snapshot"] = json.loads(out.get("book_snapshot") or "{}")
    except Exception:
        out["book_snapshot"] = {}
    out["emails"] = [dict(r) for r in rows]
    return out


def find_email_by_mailerlite_id(campaign_id: str) -> Optional[dict[str, Any]]:
    """Reverse-lookup: given a MailerLite campaign id, return the matching
    email row (so we can recover the publication_id + position).

    Returns None if no draft was ever pushed for that id.
    """
    if not campaign_id:
        return None
    needle = f"mailerlite:{campaign_id}"
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM emails WHERE pushed_to=? LIMIT 1", (needle,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def delete_email_campaign(publication_id: str) -> bool:
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM emails WHERE publication_id=?", (publication_id,))
        cur = conn.execute(
            "DELETE FROM email_campaigns WHERE publication_id=?",
            (publication_id,),
        )
        conn.commit()
        conn.close()
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Generic settings (key/value) — MailerLite credentials and future integrations
# ---------------------------------------------------------------------------


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return default
    value = row["value"]
    return default if (value is None or value == "") else value


def set_setting(key: str, value: Optional[str]) -> None:
    """Upsert a setting. Empty/None deletes the row."""
    now = time.time()
    with _lock:
        conn = _connect()
        if value is None or value == "":
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            conn.execute(
                """
                INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                               updated_at=excluded.updated_at
                """,
                (key, value, now),
            )
        conn.commit()
        conn.close()


def get_settings_with_prefix(prefix: str) -> dict[str, str]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE ?",
            (prefix + "%",),
        ).fetchall()
    finally:
        conn.close()
    return {r["key"]: r["value"] for r in rows}


# ---------------------------------------------------------------------------
# Trivia & facts books
#
# Kept in their own table and helper block so the trivia generator can evolve
# without touching the prose-book queries above.
# ---------------------------------------------------------------------------

def save_trivia_book(
    book_id: str,
    title: str,
    topic: str,
    *,
    status: str = "queued",
    agent: str = "main",
    difficulty: str = "medium",
    answer_key_position: str = "end_of_book",
    chapter_count: int = 0,
    trivia_total: int = 0,
    fact_total: int = 0,
    config_json: str = "",
) -> None:
    now = time.time()
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO trivia_books(
                id, title, topic, status, agent, difficulty, answer_key_position,
                chapter_count, trivia_total, fact_total, config_json,
                created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                topic=excluded.topic,
                status=excluded.status,
                agent=excluded.agent,
                difficulty=excluded.difficulty,
                answer_key_position=excluded.answer_key_position,
                chapter_count=excluded.chapter_count,
                trivia_total=excluded.trivia_total,
                fact_total=excluded.fact_total,
                config_json=excluded.config_json,
                updated_at=excluded.updated_at
            """,
            (
                book_id, title, topic, status, agent, difficulty,
                answer_key_position, chapter_count, trivia_total, fact_total,
                config_json, now, now,
            ),
        )
        conn.commit()
        conn.close()


_TRIVIA_UPDATABLE = {
    "title", "topic", "status", "stage", "progress", "agent", "difficulty",
    "answer_key_position", "chapter_count", "trivia_total", "fact_total",
    "config_json", "json_path", "markdown_path", "docx_path", "kindle_path",
    "paperback_path", "error", "warnings_json", "usage_json",
}


def update_trivia_book(book_id: str, **fields: Any) -> None:
    allowed = {k: v for k, v in fields.items() if k in _TRIVIA_UPDATABLE}
    if not allowed:
        return
    sets = ", ".join(f"{k}=?" for k in allowed)
    values = list(allowed.values()) + [time.time(), book_id]
    with _lock:
        conn = _connect()
        conn.execute(
            f"UPDATE trivia_books SET {sets}, updated_at=? WHERE id=?",
            values,
        )
        conn.commit()
        conn.close()


def list_trivia_books(limit: int = 50) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, title, topic, status, stage, progress, difficulty,
                   answer_key_position, chapter_count, trivia_total, fact_total,
                   json_path, markdown_path, docx_path, kindle_path,
                   paperback_path, error, created_at, updated_at,
                   -- Whether the book can be re-run, without shipping the whole
                   -- config blob to the library view for every row.
                   (config_json IS NOT NULL AND config_json != '') AS has_config
            FROM trivia_books
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_trivia_book(book_id: str) -> Optional[dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM trivia_books WHERE id=?", (book_id,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def delete_trivia_book(book_id: str) -> bool:
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM trivia_books WHERE id=?", (book_id,))
        conn.commit()
        deleted = cur.rowcount > 0
        conn.close()
    return deleted


# ---------------------------------------------------------------------------
# Puzzle & activity books
#
# Own table and helper block, so the puzzle generator can evolve without
# touching the prose-book or trivia queries above.
# ---------------------------------------------------------------------------

def save_puzzle_book(
    book_id: str,
    title: str,
    topic: str,
    *,
    status: str = "queued",
    agent: str = "main",
    audience: str = "",
    difficulty: str = "medium",
    counts_json: str = "",
    estimated_pages: int = 0,
    config_json: str = "",
) -> None:
    now = time.time()
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO puzzle_books(
                id, title, topic, status, agent, audience, difficulty,
                counts_json, estimated_pages, config_json, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                topic=excluded.topic,
                status=excluded.status,
                agent=excluded.agent,
                audience=excluded.audience,
                difficulty=excluded.difficulty,
                counts_json=excluded.counts_json,
                estimated_pages=excluded.estimated_pages,
                config_json=excluded.config_json,
                updated_at=excluded.updated_at
            """,
            (
                book_id, title, topic, status, agent, audience, difficulty,
                counts_json, estimated_pages, config_json, now, now,
            ),
        )
        conn.commit()
        conn.close()


_PUZZLE_UPDATABLE = {
    "title", "topic", "status", "stage", "progress", "agent", "audience",
    "difficulty", "counts_json", "estimated_pages", "config_json", "json_path",
    "markdown_path", "docx_path", "kindle_path", "paperback_path", "zip_path",
    "error", "warnings_json", "usage_json",
}


def update_puzzle_book(book_id: str, **fields: Any) -> None:
    allowed = {k: v for k, v in fields.items() if k in _PUZZLE_UPDATABLE}
    if not allowed:
        return
    sets = ", ".join(f"{k}=?" for k in allowed)
    values = list(allowed.values()) + [time.time(), book_id]
    with _lock:
        conn = _connect()
        conn.execute(
            f"UPDATE puzzle_books SET {sets}, updated_at=? WHERE id=?",
            values,
        )
        conn.commit()
        conn.close()


def list_puzzle_books(limit: int = 50) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, title, topic, status, stage, progress, audience,
                   difficulty, counts_json, estimated_pages, json_path,
                   markdown_path, docx_path, kindle_path, paperback_path,
                   zip_path, error, created_at, updated_at,
                   -- Whether the book can be re-run, without shipping the whole
                   -- config blob to the library view for every row.
                   (config_json IS NOT NULL AND config_json != '') AS has_config
            FROM puzzle_books
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_puzzle_book(book_id: str) -> Optional[dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM puzzle_books WHERE id=?", (book_id,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def delete_puzzle_book(book_id: str) -> bool:
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM puzzle_books WHERE id=?", (book_id,))
        conn.commit()
        deleted = cur.rowcount > 0
        conn.close()
    return deleted


# ---------------------------------------------------------------------------
# Researched-stories books
#
# Own table and helper block, so the stories generator can evolve without
# touching the prose-book, trivia or puzzle queries above.
# ---------------------------------------------------------------------------

def save_story_book(
    book_id: str,
    title: str,
    topic: str,
    *,
    status: str = "queued",
    agent: str = "main",
    audience: str = "",
    tone: str = "",
    story_count: int = 0,
    min_words: int = 300,
    max_words: int = 500,
    config_json: str = "",
) -> None:
    now = time.time()
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO story_books(
                id, title, topic, status, agent, audience, tone,
                story_count, min_words, max_words, config_json,
                created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                topic=excluded.topic,
                status=excluded.status,
                agent=excluded.agent,
                audience=excluded.audience,
                tone=excluded.tone,
                story_count=excluded.story_count,
                min_words=excluded.min_words,
                max_words=excluded.max_words,
                config_json=excluded.config_json,
                updated_at=excluded.updated_at
            """,
            (
                book_id, title, topic, status, agent, audience, tone,
                story_count, min_words, max_words, config_json, now, now,
            ),
        )
        conn.commit()
        conn.close()


_STORY_UPDATABLE = {
    "title", "topic", "status", "stage", "progress", "agent", "audience",
    "tone", "story_count", "stories_written", "total_words", "min_words",
    "max_words", "config_json", "json_path", "markdown_path", "docx_path",
    "kindle_path", "paperback_path", "factcheck_path", "error",
    "warnings_json", "usage_json",
}


def update_story_book(book_id: str, **fields: Any) -> None:
    allowed = {k: v for k, v in fields.items() if k in _STORY_UPDATABLE}
    if not allowed:
        return
    sets = ", ".join(f"{k}=?" for k in allowed)
    values = list(allowed.values()) + [time.time(), book_id]
    with _lock:
        conn = _connect()
        conn.execute(
            f"UPDATE story_books SET {sets}, updated_at=? WHERE id=?",
            values,
        )
        conn.commit()
        conn.close()


def list_story_books(limit: int = 50) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, title, topic, status, stage, progress, audience, tone,
                   story_count, stories_written, total_words, min_words,
                   max_words, json_path, markdown_path, docx_path, kindle_path,
                   paperback_path, factcheck_path, error, created_at, updated_at
            FROM story_books
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_story_book(book_id: str) -> Optional[dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM story_books WHERE id=?", (book_id,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def delete_story_book(book_id: str) -> bool:
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM story_books WHERE id=?", (book_id,))
        conn.commit()
        deleted = cur.rowcount > 0
        conn.close()
    return deleted

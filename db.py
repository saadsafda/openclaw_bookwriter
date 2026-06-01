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

        # Amazon Ads accounts (one row per company / LWA refresh-token).
        # Each account hosts multiple marketplace profiles (US/UK/CA/AU…).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS amazon_ads_accounts (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                lwa_refresh_token TEXT NOT NULL,
                env TEXT NOT NULL DEFAULT 'production',
                client_id TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.commit()
        # Seed the .env token as the "default" account on first run so the
        # existing single-account install keeps working without manual steps.
        existing_acct = conn.execute(
            "SELECT count(*) FROM amazon_ads_accounts"
        ).fetchone()[0]
        if existing_acct == 0:
            env_token = os.getenv("LWA_REFRESH_TOKEN", "").strip()
            if env_token:
                now = time.time()
                conn.execute(
                    "INSERT INTO amazon_ads_accounts "
                    "(id, label, lwa_refresh_token, env, client_id, "
                    " notes, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "default",
                        "Default",
                        env_token,
                        os.getenv("AMAZON_ADS_ENV", "production").strip().lower() or "production",
                        os.getenv("LWA_CLIENT_ID", "").strip(),
                        "Seeded from .env on first startup.",
                        now, now,
                    ),
                )
                conn.commit()

        # Amazon Ads campaigns we launched via the API.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS amazon_ads_campaigns (
                campaign_id TEXT PRIMARY KEY,
                book_id TEXT NOT NULL DEFAULT '',
                publication_id TEXT NOT NULL DEFAULT '',
                marketplace TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                campaign_type TEXT NOT NULL,
                name TEXT NOT NULL,
                asins TEXT NOT NULL DEFAULT '[]',
                ad_group_id TEXT NOT NULL DEFAULT '',
                product_ad_ids TEXT NOT NULL DEFAULT '[]',
                keyword_ids TEXT NOT NULL DEFAULT '[]',
                negative_keyword_ids TEXT NOT NULL DEFAULT '[]',
                target_ids TEXT NOT NULL DEFAULT '[]',
                daily_budget REAL NOT NULL DEFAULT 0,
                default_bid REAL NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'PAUSED',
                payload TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_amazon_ads_campaigns_book
            ON amazon_ads_campaigns(book_id, created_at DESC)
        """)
        # Migration: ensure publication_id column exists on older DBs
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(amazon_ads_campaigns)").fetchall()}
        if "publication_id" not in cols:
            conn.execute(
                "ALTER TABLE amazon_ads_campaigns "
                "ADD COLUMN publication_id TEXT NOT NULL DEFAULT ''"
            )
        if "amazon_account_id" not in cols:
            conn.execute(
                "ALTER TABLE amazon_ads_campaigns "
                "ADD COLUMN amazon_account_id TEXT NOT NULL DEFAULT ''"
            )
        if "bidding_strategy" not in cols:
            conn.execute(
                "ALTER TABLE amazon_ads_campaigns "
                "ADD COLUMN bidding_strategy TEXT NOT NULL DEFAULT 'LEGACY_FOR_SALES'"
            )
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_amazon_ads_campaigns_pub
            ON amazon_ads_campaigns(publication_id, created_at DESC)
        """)
        # Migration: ensure amazon_account_id column exists on publications
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

# ----- Amazon Ads campaigns -----

def save_amazon_campaign(
    *,
    campaign_id: str,
    book_id: str,
    marketplace: str,
    profile_id: str,
    campaign_type: str,
    name: str,
    asins: list[str],
    ad_group_id: str = "",
    product_ad_ids: list[str] | None = None,
    keyword_ids: list[str] | None = None,
    negative_keyword_ids: list[str] | None = None,
    target_ids: list[str] | None = None,
    daily_budget: float = 0.0,
    default_bid: float = 0.0,
    state: str = "PAUSED",
    payload: dict[str, Any] | None = None,
    publication_id: str = "",
    amazon_account_id: str = "",
    bidding_strategy: str = "LEGACY_FOR_SALES",
) -> None:
    """Persist a campaign launched via the Amazon Ads API."""
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO amazon_ads_campaigns (
                campaign_id, book_id, publication_id, marketplace, profile_id,
                campaign_type, name,
                asins, ad_group_id, product_ad_ids, keyword_ids,
                negative_keyword_ids, target_ids, daily_budget, default_bid,
                state, payload, created_at,
                amazon_account_id, bidding_strategy
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(campaign_id), book_id, publication_id,
                marketplace, str(profile_id),
                campaign_type, name,
                json.dumps(asins),
                ad_group_id,
                json.dumps(product_ad_ids or []),
                json.dumps(keyword_ids or []),
                json.dumps(negative_keyword_ids or []),
                json.dumps(target_ids or []),
                float(daily_budget),
                float(default_bid),
                state,
                json.dumps(payload or {}),
                time.time(),
                amazon_account_id,
                bidding_strategy,
            ),
        )
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# Amazon Ads accounts (companies)
# ---------------------------------------------------------------------------


def list_amazon_ads_accounts(*, include_token: bool = False) -> list[dict[str, Any]]:
    """List configured Amazon Ads accounts (companies). Tokens stripped by default."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM amazon_ads_accounts ORDER BY created_at ASC"
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        if not include_token:
            d.pop("lwa_refresh_token", None)
        out.append(d)
    return out


def get_amazon_ads_account(
    account_id: str, *, include_token: bool = True
) -> Optional[dict[str, Any]]:
    """Fetch one account by id. Includes the refresh token by default (callers need it)."""
    if not account_id:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM amazon_ads_accounts WHERE id=?", (account_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    d = dict(row)
    if not include_token:
        d.pop("lwa_refresh_token", None)
    return d


def save_amazon_ads_account(
    *,
    label: str,
    lwa_refresh_token: str,
    env: str = "production",
    client_id: str = "",
    notes: str = "",
    account_id: str | None = None,
) -> str:
    """Upsert an Amazon Ads account. Returns the account id."""
    if not label.strip():
        raise ValueError("label is required")
    if not lwa_refresh_token.strip():
        raise ValueError("lwa_refresh_token is required")
    acct_id = (account_id or uuid.uuid4().hex[:12]).strip()
    now = time.time()
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO amazon_ads_accounts
                (id, label, lwa_refresh_token, env, client_id, notes,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                label=excluded.label,
                lwa_refresh_token=excluded.lwa_refresh_token,
                env=excluded.env,
                client_id=excluded.client_id,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
            (
                acct_id, label.strip(), lwa_refresh_token.strip(),
                (env or "production").strip().lower() or "production",
                client_id.strip(), notes.strip(), now, now,
            ),
        )
        conn.commit()
        conn.close()
    return acct_id


def delete_amazon_ads_account(account_id: str) -> bool:
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "DELETE FROM amazon_ads_accounts WHERE id=?", (account_id,)
        )
        conn.commit()
        conn.close()
    return cur.rowcount > 0


def get_default_amazon_ads_account_id() -> Optional[str]:
    """Return the id of the first configured account, or None."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id FROM amazon_ads_accounts ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return row["id"] if row else None


def list_amazon_campaigns(
    book_id: str | None = None,
    publication_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return campaigns, optionally filtered by book or publication, newest first."""
    conn = _connect()
    if publication_id:
        rows = conn.execute(
            "SELECT * FROM amazon_ads_campaigns WHERE publication_id=? "
            "ORDER BY created_at DESC LIMIT ?",
            (publication_id, limit),
        ).fetchall()
    elif book_id:
        rows = conn.execute(
            "SELECT * FROM amazon_ads_campaigns WHERE book_id=? "
            "ORDER BY created_at DESC LIMIT ?",
            (book_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM amazon_ads_campaigns ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        for k in ("asins", "product_ad_ids", "keyword_ids",
                  "negative_keyword_ids", "target_ids", "payload"):
            try:
                d[k] = json.loads(d.get(k) or ("{}" if k == "payload" else "[]"))
            except Exception:
                pass
        out.append(d)
    return out


def record_amazon_campaign_result(
    book_id: str, result: dict[str, Any], *,
    daily_budget: float, default_bid: float, name: str, state: str = "PAUSED",
) -> None:
    """Convenience helper to persist the dict returned by campaigns.create_*_campaign()."""
    save_amazon_campaign(
        campaign_id=str(result.get("campaignId", "")),
        book_id=book_id,
        marketplace=result.get("marketplace", ""),
        profile_id=str(result.get("profileId", "")),
        campaign_type=result.get("type", ""),
        name=name,
        asins=list(result.get("asins", []) or []) or [],
        ad_group_id=str(result.get("adGroupId", "")),
        product_ad_ids=list(result.get("productAdIds", []) or []),
        keyword_ids=list(result.get("keywordIds", []) or []),
        negative_keyword_ids=list(result.get("negativeKeywordIds", []) or []),
        target_ids=list(result.get("targetIds", []) or []),
        daily_budget=daily_budget,
        default_bid=default_bid,
        state=state,
        payload=result,
    )

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

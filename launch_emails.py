"""Launch-email campaigns: per-publication 3/4/5-email drafts pushed to MailerLite.

Lifecycle:
  1. POST /api/publications/<pub_id>/launch-emails/generate
     - reads the publication's kindle_docx, asks the email-launch-agent for an
       N-email sequence with day-offsets, saves the drafts in our DB.
  2. PATCH/PUT to edit subject/body/cta/day-offset per email.
  3. POST /api/publications/<pub_id>/launch-emails/<position>/push
     - converts markdown body to HTML, swaps [BOOK_LINK] for the publication's
       Amazon URL, creates a *draft* campaign in MailerLite. Never sends.

Hard rule: this module never sends email. Everything lands as a MailerLite draft.
The user reviews and presses publish manually inside MailerLite.

Routes are registered on the Flask app via ``register(app)``.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from flask import abort, jsonify, request

import db as bookdb
import email_agent
import mailerlite_client
import review_automation


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settings keys for MailerLite. Stored in the generic settings table.
# ---------------------------------------------------------------------------

_ML_KEYS = {
    "api_key":          "mailerlite.api_key",
    "from_email":       "mailerlite.from_email",
    "from_name":        "mailerlite.from_name",
    "default_group_id": "mailerlite.default_group_id",
    "webhook_secret":   "mailerlite.webhook_secret",
}

# Per-company (publisher) MailerLite config lives under scoped keys so each
# Amazon Ads account / publisher can point at its own MailerLite account or
# draft-access sub-user. A book published under "Oak Harbor Press" uses Oak
# Harbor's list; a different publisher uses its own — with the global keys
# above acting as the shared default (today: everything runs on Oak Harbor as
# the global default). Adding a publisher later is one settings row, no code.
#
#   global:       mailerlite.api_key
#   per-company:  mailerlite.<account_id>.api_key
#
# webhook_secret is intentionally global only — there's a single webhook
# endpoint and we can't know the company at delivery time.
_ML_COMPANY_FIELDS = ("api_key", "from_email", "from_name", "default_group_id")


def _ml_company_key(field: str, account_id: str) -> str:
    return f"mailerlite.{account_id}.{field}"


def _ml_setting(field: str, account_id: str | None = None) -> str:
    """Resolve a MailerLite setting: per-company → global → env (api_key only).

    This is the single source of truth for which credentials a given publisher
    uses. Pass the publication's company (``account_id``) and you get that
    company's value, falling back to the global Oak Harbor default.
    """
    if account_id and field in _ML_COMPANY_FIELDS:
        scoped = bookdb.get_setting(_ml_company_key(field, account_id))
        if scoped:
            return scoped
    glob = bookdb.get_setting(_ML_KEYS[field]) or ""
    if glob:
        return glob
    if field == "api_key":
        return os.environ.get("MAILERLITE_API_KEY", "")
    return ""


def _account_for_pub(pub: dict) -> str:
    """Which company (Amazon Ads account / publisher) owns this publication."""
    return (str(pub.get("amazon_account_id") or "").strip()
            or bookdb.get_default_amazon_ads_account_id()
            or "")


# ---------------------------------------------------------------------------
# Webhook signature verification + event parsing
# ---------------------------------------------------------------------------

# Event names we treat as "the subscriber clicked a link in a campaign email."
# MailerLite has evolved this name across API versions, so allow several.
_CLICK_EVENT_NAMES = (
    "subscriber.email_link_clicked",
    "subscriber.clicked_email_link",
    "subscriber.email_clicked",
    "campaign.email_link_clicked",
    "campaign.clicked",
    "campaign.email_clicked",
)


def _verify_webhook_signature(raw_body: bytes, supplied: str | None,
                              secret: str) -> bool:
    """Verify the incoming MailerLite webhook signature using HMAC-SHA256.

    Returns True if `secret` is empty (no verification configured) — the
    caller decides whether to warn or reject in that case.
    """
    if not secret:
        return True
    if not supplied:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256,
    ).hexdigest()
    # Constant-time compare; accept hex form
    return hmac.compare_digest(expected, supplied.strip().lower())


def _is_click_event(event_name: str) -> bool:
    name = (event_name or "").lower()
    if name in _CLICK_EVENT_NAMES:
        return True
    # Defensive: anything that mentions both 'click' and 'email' or 'campaign'.
    if "click" in name and ("email" in name or "campaign" in name or "link" in name):
        return True
    return False


def _normalize_events(payload: Any) -> list[dict[str, Any]]:
    """MailerLite v1 sent ``{"events":[...]}``; later versions sometimes send a
    single event object at the top level. Normalize both shapes to a list.
    """
    if isinstance(payload, dict):
        if isinstance(payload.get("events"), list):
            return [e for e in payload["events"] if isinstance(e, dict)]
        # Single-event object
        if payload.get("name") or payload.get("event") or payload.get("type"):
            return [payload]
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)]
    return []


def _extract_click_facts(event: dict[str, Any]) -> dict[str, Any] | None:
    """Pull (subscriber_email, subscriber_name, campaign_id, click_ts) out of
    an event dict, regardless of which API version produced it.

    Returns None if any required field is missing.
    """
    name = (event.get("name") or event.get("event") or event.get("type") or "")
    if not _is_click_event(name):
        return None
    data = event.get("data") or event
    subscriber = data.get("subscriber") or {}
    campaign = data.get("campaign") or {}
    email = (subscriber.get("email") or "").strip().lower()
    sub_name = (subscriber.get("name")
                or subscriber.get("fields", {}).get("name", "")
                or "").strip()
    campaign_id = str(campaign.get("id") or campaign.get("campaign_id") or "").strip()
    ts_raw = (event.get("timestamp") or event.get("created_at")
              or data.get("timestamp") or "")
    click_ts = _parse_timestamp(ts_raw)
    if not (email and campaign_id):
        return None
    return {
        "email": email,
        "name": sub_name,
        "campaign_id": campaign_id,
        "click_ts": click_ts,
        "raw_event_name": name,
    }


def _parse_timestamp(ts: Any) -> float:
    """Best-effort: parse ISO8601 / unix epoch / unknown into a unix timestamp."""
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str) and ts:
        # Unix-as-string
        try:
            return float(ts)
        except ValueError:
            pass
        # ISO format
        try:
            from datetime import datetime
            # Tolerate trailing Z
            cleaned = ts.replace("Z", "+00:00")
            return datetime.fromisoformat(cleaned).timestamp()
        except Exception:
            pass
    return time.time()


def _ml_get_client(account_id: str | None = None) -> mailerlite_client.MailerLiteClient:
    """Build a MailerLite client for a company; 400 if unconfigured.

    Pass the publication's company (``account_id``) to use that publisher's
    MailerLite account, falling back to the global Oak Harbor default.
    """
    api_key = _ml_setting("api_key", account_id)
    if not api_key:
        abort(400, description=(
            "MailerLite API key not configured. Add it via PUT "
            "/api/settings/mailerlite."
        ))
    try:
        return mailerlite_client.MailerLiteClient(api_key)
    except mailerlite_client.MailerLiteError as exc:
        abort(400, description=str(exc))


def _ml_markdown_to_html(md: str, cta_url: str = "",
                         cta_label: str = "Read the Book",
                         mockup_url: str = "") -> str:
    """Convert email markdown body to HTML.

    Substitutions:
      [BOOK_LINK]    → the Amazon Kindle product URL (anchor)
      [COVER_MOCKUP] → centered <img> of the 3D cover render; stripped if none yet
    """
    if not md:
        return ""

    # Replace the mockup token at the markdown level so it survives conversion.
    if mockup_url:
        mockup_html = (
            '\n\n<p style="text-align:center;margin:18px 0;">'
            f'<img src="{mockup_url}" alt="Book cover" '
            'style="max-width:320px;width:100%;height:auto;border:0;" />'
            '</p>\n\n'
        )
    else:
        mockup_html = ""  # no mockup yet → remove the placeholder cleanly
    md = md.replace("[COVER_MOCKUP]", mockup_html)

    try:
        import markdown as _md  # type: ignore
    except Exception:
        import html as _html
        escaped = _html.escape(md)
        paragraphs = [p.strip() for p in escaped.split("\n\n") if p.strip()]
        html_out = "\n".join(
            f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs
        )
    else:
        html_out = _md.markdown(md, extensions=["extra", "nl2br", "sane_lists"])

    if cta_url:
        link_html = (
            f'<a href="{cta_url}" target="_blank" rel="noopener">'
            f'{cta_label or "Read the Book"}</a>'
        )
    else:
        link_html = '<a href="#">[Add your book link]</a>'
    return html_out.replace("[BOOK_LINK]", link_html)


# ---------------------------------------------------------------------------
# Launch-date helpers (used to compute absolute send dates per offset)
# ---------------------------------------------------------------------------


def _compute_launch_base(
    launch_date: str, launch_time: str, launch_timezone: str
):
    """Return a tz-aware datetime, or None if incomplete/invalid."""
    if not (launch_date and launch_time and launch_timezone):
        return None
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except Exception:
        return None
    try:
        tz = ZoneInfo(launch_timezone)
    except ZoneInfoNotFoundError:
        return None
    try:
        base = datetime.strptime(
            f"{launch_date} {launch_time}", "%Y-%m-%d %H:%M"
        )
    except ValueError:
        return None
    return base.replace(tzinfo=tz)


def _serialize_campaign(campaign: dict[str, Any] | None) -> dict[str, Any]:
    if not campaign:
        return {
            "exists": False, "emails": [],
            "schedule": {"launch_date": "", "launch_time": "", "launch_timezone": "",
                         "is_set": False},
        }
    launch_date = campaign.get("launch_date") or ""
    launch_time = campaign.get("launch_time") or ""
    launch_timezone = campaign.get("launch_timezone") or ""
    base_dt = _compute_launch_base(launch_date, launch_time, launch_timezone)

    def _compute_send(day_offset: int) -> dict[str, str]:
        if base_dt is None:
            return {"send_datetime_iso": "", "send_date_iso": "", "send_label": ""}
        from datetime import timedelta
        try:
            dt = base_dt + timedelta(days=int(day_offset or 0))
        except (TypeError, ValueError):
            return {"send_datetime_iso": "", "send_date_iso": "", "send_label": ""}
        label = dt.strftime("%a %b %d %Y · %-I:%M %p %Z") if dt.tzinfo \
                else dt.strftime("%a %b %d %Y · %-I:%M %p")
        return {
            "send_datetime_iso": dt.isoformat(),
            "send_date_iso": dt.date().isoformat(),
            "send_label": label,
        }

    return {
        "exists": True,
        "status": campaign.get("status") or "draft",
        "format": campaign.get("format") or "markdown",
        "sequence_length": campaign.get("sequence_length") or 0,
        "error": campaign.get("error") or "",
        "created_at": campaign.get("created_at"),
        "updated_at": campaign.get("updated_at"),
        "book_snapshot": campaign.get("book_snapshot") or {},
        "schedule": {
            "launch_date": launch_date,
            "launch_time": launch_time,
            "launch_timezone": launch_timezone,
            "is_set": base_dt is not None,
        },
        "emails": [
            {
                "position":   e.get("position"),
                "day_offset": e.get("day_offset"),
                "subject":    e.get("subject") or "",
                "preview":    e.get("preview") or "",
                "body":       e.get("body") or "",
                "cta_label":  e.get("cta_label") or "",
                "cta_url":    e.get("cta_url") or "",
                "pushed_to":  e.get("pushed_to") or "",
                "updated_at": e.get("updated_at"),
                **_compute_send(e.get("day_offset") or 0),
            }
            for e in (campaign.get("emails") or [])
        ],
    }


# ---------------------------------------------------------------------------
# Background generation worker
# ---------------------------------------------------------------------------


def _run_generate(
    pub_id: str, count: int, agent_id: str | None = None,
    timeout_s: int | None = None,
) -> None:
    """Run email_agent.generate_email_campaign in a thread and persist results."""
    pub = bookdb.get_publication(pub_id)
    if not pub:
        return
    kindle = pub.get("kindle_docx_path") or ""
    if not kindle or not Path(kindle).exists():
        bookdb.save_email_campaign(
            pub_id, emails=[], status="error",
            error="Publication has no kindle_docx_path or file not found.",
        )
        return

    # Mark "running"
    bookdb.save_email_campaign(
        pub_id, emails=[], status="generating", error="",
        book_snapshot={"started_at": "running"},
    )

    try:
        kwargs: dict[str, Any] = dict(
            docx_path=Path(kindle),
            title=(pub.get("title") or "").strip(),
            count=count,
            description=str(pub.get("description") or ""),
            subtitles=([pub.get("subtitle")] if pub.get("subtitle") else []),
        )
        if agent_id:
            kwargs["agent_id"] = agent_id
        if timeout_s and timeout_s > 0:
            kwargs["timeout_s"] = int(timeout_s)
        result = email_agent.generate_email_campaign(**kwargs)
        emails_payload = [
            {
                "position":   e.position,
                "day_offset": e.day_offset,
                "subject":    e.subject,
                "preview":    e.preview,
                "body":       e.body,
                "cta_label":  e.cta_label,
            }
            for e in result.emails
        ]
        bookdb.save_email_campaign(
            pub_id, emails=emails_payload, status="draft", format="markdown",
            book_snapshot=result.book_snapshot, error="",
        )
    except Exception as exc:
        bookdb.save_email_campaign(
            pub_id, emails=[], status="error", error=str(exc),
        )


# ---------------------------------------------------------------------------
# Flask registration
# ---------------------------------------------------------------------------


def register(app) -> None:  # noqa: ANN001
    """Attach all launch-email + MailerLite routes."""

    # ---- Per-publication campaign CRUD ----------------------------------

    @app.post("/api/publications/<pub_id>/launch-emails/generate")
    def generate_emails(pub_id: str):  # noqa: ANN202
        pub = bookdb.get_publication(pub_id)
        if not pub:
            abort(404)
        body = request.get_json(silent=True) or {}
        try:
            count = int(body.get("count", 5))
        except (TypeError, ValueError):
            count = 5
        if count not in (3, 4, 5):
            return jsonify({"error": "count must be 3, 4, or 5"}), 400
        kindle = pub.get("kindle_docx_path") or ""
        if not kindle or not Path(kindle).exists():
            return jsonify({
                "error": "publication has no kindle_docx_path or file missing",
            }), 400
        existing = bookdb.get_email_campaign(pub_id)
        if existing and existing.get("status") == "generating":
            return jsonify({"error": "already generating; wait for it to finish"}), 409
        agent_id = (body.get("agent_id") or "").strip() or None
        try:
            timeout_s = int(body.get("timeout_s") or 0) or None
        except (TypeError, ValueError):
            timeout_s = None
        t = threading.Thread(
            target=_run_generate,
            args=(pub_id, count, agent_id, timeout_s),
            daemon=True,
            name=f"launch-emails-gen-{pub_id}",
        )
        t.start()
        return jsonify({
            "ok": True, "count": count, "status": "generating",
            "agent_id": agent_id or email_agent.DEFAULT_AGENT_ID,
        })

    @app.get("/api/publications/<pub_id>/launch-emails")
    def get_emails(pub_id: str):  # noqa: ANN202
        if not bookdb.get_publication(pub_id):
            abort(404)
        camp = bookdb.get_email_campaign(pub_id)
        return jsonify(_serialize_campaign(camp))

    @app.patch("/api/publications/<pub_id>/launch-emails/<int:position>")
    def update_one_email(pub_id: str, position: int):  # noqa: ANN202
        if not bookdb.get_email_campaign(pub_id):
            return jsonify({"error": "no email campaign for this publication"}), 404
        body = request.get_json(silent=True) or {}
        allowed = {"subject", "preview", "body", "cta_label", "cta_url", "day_offset"}
        updates: dict[str, Any] = {}
        for k in allowed:
            if k not in body:
                continue
            if k == "day_offset":
                try:
                    updates[k] = int(body[k])
                except (TypeError, ValueError):
                    return jsonify({"error": "day_offset must be an integer"}), 400
            else:
                updates[k] = str(body[k])
        if not updates:
            return jsonify({"error": "no editable fields provided"}), 400
        if not bookdb.update_email(pub_id, position, **updates):
            return jsonify({"error": f"email {position} not found"}), 404
        return jsonify(_serialize_campaign(bookdb.get_email_campaign(pub_id)))

    @app.delete("/api/publications/<pub_id>/launch-emails")
    def delete_emails(pub_id: str):  # noqa: ANN202
        deleted = bookdb.delete_email_campaign(pub_id)
        return jsonify({"ok": True, "deleted": deleted})

    @app.put("/api/publications/<pub_id>/launch-emails/schedule")
    def set_schedule(pub_id: str):  # noqa: ANN202
        if not bookdb.get_email_campaign(pub_id):
            return jsonify({"error": "no email campaign for this publication"}), 404
        body = request.get_json(silent=True) or {}
        launch_date = str(body.get("launch_date") or "").strip()
        launch_time = str(body.get("launch_time") or "").strip()
        launch_timezone = str(body.get("launch_timezone") or "").strip()
        if not (launch_date or launch_time or launch_timezone):
            bookdb.update_email_campaign(
                pub_id, launch_date="", launch_time="", launch_timezone="",
            )
            return jsonify(_serialize_campaign(bookdb.get_email_campaign(pub_id)))
        if not (launch_date and launch_time and launch_timezone):
            return jsonify({
                "error": "launch_date, launch_time, and launch_timezone all required",
            }), 400
        from datetime import datetime
        try:
            datetime.strptime(launch_date, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "launch_date must be YYYY-MM-DD"}), 400
        try:
            datetime.strptime(launch_time, "%H:%M")
        except ValueError:
            return jsonify({"error": "launch_time must be HH:MM (24h)"}), 400
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            ZoneInfo(launch_timezone)
        except ZoneInfoNotFoundError:
            return jsonify({
                "error": f"unknown timezone '{launch_timezone}' (use IANA name)",
            }), 400
        bookdb.update_email_campaign(
            pub_id,
            launch_date=launch_date,
            launch_time=launch_time,
            launch_timezone=launch_timezone,
        )
        return jsonify(_serialize_campaign(bookdb.get_email_campaign(pub_id)))

    # ---- MailerLite settings + management -------------------------------

    def _ml_settings_payload(account_id: str = ""):
        """Build the settings response for the global default or one company.

        For a company (``account_id`` set), each field shows that company's own
        stored value, plus an ``inherited`` block showing the global value that
        would be used as fallback when the company's field is blank.
        """
        if account_id:
            own_key = bookdb.get_setting(_ml_company_key("api_key", account_id)) or ""
            glob_key = bookdb.get_setting(_ML_KEYS["api_key"]) or ""
            env_key = os.environ.get("MAILERLITE_API_KEY", "")
            return {
                "scope": "company",
                "account_id": account_id,
                "configured": bool(own_key or glob_key or env_key),
                "api_key_set": bool(own_key),          # company-specific key present?
                "from_email":       bookdb.get_setting(_ml_company_key("from_email", account_id)) or "",
                "from_name":        bookdb.get_setting(_ml_company_key("from_name", account_id)) or "",
                "default_group_id": bookdb.get_setting(_ml_company_key("default_group_id", account_id)) or "",
                "inherited": {
                    "api_key_set":      bool(glob_key or env_key),
                    "from_email":       bookdb.get_setting(_ML_KEYS["from_email"]) or "",
                    "from_name":        bookdb.get_setting(_ML_KEYS["from_name"]) or "",
                    "default_group_id": bookdb.get_setting(_ML_KEYS["default_group_id"]) or "",
                },
            }
        stored = bookdb.get_setting(_ML_KEYS["api_key"]) or ""
        env_key = os.environ.get("MAILERLITE_API_KEY", "")
        return {
            "scope": "global",
            "account_id": "",
            "configured": bool(stored or env_key),
            "source": "database" if stored else ("env" if env_key else "none"),
            "api_key_set":        bool(stored),
            "from_email":         bookdb.get_setting(_ML_KEYS["from_email"]) or "",
            "from_name":          bookdb.get_setting(_ML_KEYS["from_name"]) or "",
            "default_group_id":   bookdb.get_setting(_ML_KEYS["default_group_id"]) or "",
            "webhook_secret_set": bool(bookdb.get_setting(_ML_KEYS["webhook_secret"])),
        }

    @app.get("/api/settings/mailerlite")
    def get_ml_settings():  # noqa: ANN202
        account_id = (request.args.get("account_id") or "").strip()
        return jsonify(_ml_settings_payload(account_id))

    @app.put("/api/settings/mailerlite")
    def put_ml_settings():  # noqa: ANN202
        body = request.get_json(silent=True) or {}
        account_id = (body.get("account_id") or "").strip()

        if account_id:
            # Per-company config. webhook_secret stays global (single endpoint).
            for field in _ML_COMPANY_FIELDS:
                if field in body:
                    val = body.get(field)
                    val = "" if val is None else (val if isinstance(val, str) else str(val))
                    bookdb.set_setting(_ml_company_key(field, account_id), val.strip())
        else:
            for field in ("api_key", "from_email", "from_name",
                          "default_group_id", "webhook_secret"):
                if field in body:
                    val = body.get(field)
                    val = "" if val is None else (val if isinstance(val, str) else str(val))
                    bookdb.set_setting(_ML_KEYS[field], val.strip())
        return jsonify(_ml_settings_payload(account_id))

    @app.post("/api/mailerlite/test")
    def test_ml():  # noqa: ANN202
        account_id = ((request.get_json(silent=True) or {}).get("account_id")
                      or request.args.get("account_id") or "").strip()
        client = _ml_get_client(account_id or None)
        try:
            client.test_connection()
        except mailerlite_client.MailerLiteError as exc:
            return jsonify({"ok": False, "error": str(exc),
                            "status_code": exc.status_code}), 400
        return jsonify({"ok": True})

    @app.get("/api/mailerlite/groups")
    def list_ml_groups():  # noqa: ANN202
        account_id = (request.args.get("account_id") or "").strip()
        client = _ml_get_client(account_id or None)
        try:
            groups = client.list_groups()
        except mailerlite_client.MailerLiteError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "groups": groups})

    # ---- Push one email to MailerLite as a draft campaign ---------------

    @app.post("/api/publications/<pub_id>/launch-emails/<int:position>/push")
    def push_email(pub_id: str, position: int):  # noqa: ANN202
        pub = bookdb.get_publication(pub_id)
        if not pub:
            abort(404)
        camp = bookdb.get_email_campaign(pub_id)
        if not camp:
            abort(404, description="no email campaign for this publication")
        email = next(
            (e for e in (camp.get("emails") or []) if e.get("position") == position),
            None,
        )
        if email is None:
            abort(404, description=f"email #{position} not found")

        body = request.get_json(silent=True) or {}

        # Which publisher (company) owns this book → which MailerLite account.
        # Explicit account_id in the request (launch dialog company dropdown) wins;
        # else resolve from the publication's pinned company.
        account_id = (str(body.get("account_id") or "").strip()
                      or _account_for_pub(pub))

        group_id = (body.get("group_id")
                    or _ml_setting("default_group_id", account_id)
                    or "").strip()
        group_ids = [group_id] if group_id else []

        from_email = (body.get("from_email")
                      or _ml_setting("from_email", account_id)
                      or "").strip()
        from_name = (body.get("from_name")
                     or _ml_setting("from_name", account_id)
                     or "").strip()
        if not from_email or not from_name:
            return jsonify({
                "ok": False,
                "error": "set a verified From email + From name in /api/settings/mailerlite first",
            }), 400

        subject = (email.get("subject") or "").strip()
        if not subject:
            return jsonify({"ok": False, "error": "email subject is empty"}), 400

        # Pull the Amazon URL for the publication's primary marketplace as the CTA target.
        primary_mp = (pub.get("primary_marketplace") or "").upper()
        mp_info = (pub.get("marketplaces") or {}).get(primary_mp) or {}
        cta_url = (email.get("cta_url") or mp_info.get("url") or "").strip()

        # 3D cover mockup, when one has been generated (point #2 — pending).
        # Accept an explicit override, then the publication's mockup, else none.
        mockup_url = (body.get("mockup_url")
                      or pub.get("cover_mockup_url")
                      or "").strip()

        html_body = _ml_markdown_to_html(
            email.get("body") or "",
            cta_url=cta_url,
            cta_label=email.get("cta_label") or "Read the Book",
            mockup_url=mockup_url,
        )

        # Friendly campaign name; embed the absolute send date if the
        # launch schedule has been set.
        title = (pub.get("title") or "Book Launch").strip()
        base_dt = _compute_launch_base(
            camp.get("launch_date") or "",
            camp.get("launch_time") or "",
            camp.get("launch_timezone") or "",
        )
        date_tag = ""
        if base_dt is not None:
            from datetime import timedelta
            try:
                send_dt = base_dt + timedelta(days=int(email.get("day_offset") or 0))
                date_tag = f" [{send_dt.strftime('%Y-%m-%d %H:%M %Z')}]"
            except (TypeError, ValueError):
                date_tag = ""
        name = f"{title} — Email {position}{date_tag}: {subject}"[:255]

        client = _ml_get_client(account_id)
        try:
            result = client.create_draft_campaign(
                name=name,
                subject=subject,
                from_email=from_email,
                from_name=from_name,
                html_content=html_body,
                group_ids=group_ids,
            )
        except mailerlite_client.MailerLiteError as exc:
            return jsonify({
                "ok": False,
                "error": str(exc),
                "status_code": exc.status_code,
            }), 400

        ml_id = result.get("id") or ""
        bookdb.update_email(pub_id, position, pushed_to=f"mailerlite:{ml_id}")
        return jsonify({
            "ok": True,
            "mailerlite_id": ml_id,
            "status": result.get("status", "draft"),
            "name": result.get("name", name),
        })

    # ---- MailerLite click webhook → review-recipient bridge -------------
    #
    # MailerLite hits this when a subscriber clicks a link in one of your
    # promo emails. We turn that click into a review_recipients row anchored
    # at click time, which causes the existing review-automation system to
    # auto-schedule the +7/+14/+30 day review-ask drafts.
    #
    # Configure in MailerLite dashboard:
    #   URL:    https://<your-host>/api/webhooks/mailerlite
    #   Event:  "subscriber.email_link_clicked" (or whatever your version
    #           calls a campaign-email click)
    #   Secret: a random string; paste the same string into our settings:
    #           PUT /api/settings/mailerlite  {"webhook_secret":"..."}
    # If no secret is configured we still accept the call but log a warning.

    @app.post("/api/webhooks/mailerlite")
    def mailerlite_webhook():  # noqa: ANN202
        raw = request.get_data() or b""
        secret = bookdb.get_setting(_ML_KEYS["webhook_secret"]) or ""
        supplied = (
            request.headers.get("X-MailerLite-Signature")
            or request.headers.get("Signature")
            or ""
        )
        if secret:
            if not _verify_webhook_signature(raw, supplied, secret):
                log.warning(
                    "MailerLite webhook signature mismatch (supplied=%r)",
                    supplied[:24],
                )
                return jsonify({"error": "signature mismatch"}), 401
        else:
            log.warning(
                "MailerLite webhook received but no webhook_secret configured "
                "— accepting unverified. Set mailerlite.webhook_secret."
            )

        try:
            payload = request.get_json(silent=True, force=True) or {}
        except Exception as exc:
            return jsonify({"error": f"bad json: {exc}"}), 400
        events = _normalize_events(payload)

        processed = 0
        bridged = 0
        ignored: list[dict[str, str]] = []
        base_url = request.url_root.rstrip("/")

        for event in events:
            processed += 1
            facts = _extract_click_facts(event)
            if facts is None:
                ignored.append({
                    "reason": "not a recognised click event",
                    "event_name": str(event.get("name")
                                      or event.get("event")
                                      or event.get("type") or ""),
                })
                continue
            row = bookdb.find_email_by_mailerlite_id(facts["campaign_id"])
            if not row:
                ignored.append({
                    "reason": "unknown MailerLite campaign id",
                    "campaign_id": facts["campaign_id"],
                })
                continue
            try:
                result = review_automation.add_recipient_and_schedule(
                    row["publication_id"],
                    email=facts["email"],
                    name=facts["name"],
                    trigger_date=facts["click_ts"],
                    source="mailerlite_click",
                    notes=f"clicked email #{row['position']} (ML id {facts['campaign_id']})",
                    base_url=base_url,
                )
                bridged += 1 if result["created"] else 0
            except ValueError as exc:
                ignored.append({
                    "reason": str(exc),
                    "email": facts["email"],
                })

        return jsonify({
            "ok": True,
            "events_received": processed,
            "recipients_created": bridged,
            "ignored": ignored,
        })

    @app.get("/api/webhooks/mailerlite/status")
    def mailerlite_webhook_status():  # noqa: ANN202
        """Tell the user whether this webhook is ready to receive events."""
        secret = bookdb.get_setting(_ML_KEYS["webhook_secret"]) or ""
        return jsonify({
            "endpoint": "/api/webhooks/mailerlite",
            "method": "POST",
            "signature_verification": "enabled" if secret else "disabled",
            "signature_header": "X-MailerLite-Signature (or Signature)",
            "expected_event_names": list(_CLICK_EVENT_NAMES),
            "note": (
                "Set mailerlite.webhook_secret in /api/settings/mailerlite "
                "(or environment) to enable signature verification."
                if not secret else
                "Signature verification is enabled. Bad signatures return 401."
            ),
        })

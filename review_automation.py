"""Review-request automation: routes, scheduler, and templates.

Surface:
    * Per-publication settings (launch date, from address, schedule, templates).
    * Recipient management (manual add + CSV upload).
    * Scheduled sends generated automatically when a recipient is added.
    * Manual send dialog (preview + .eml download + mark-sent) for users without
      SMTP credentials.
    * Optional background SMTP tick when ``SMTP_HOST`` is set in ``.env``.
    * Public ``/u/<token>`` unsubscribe route (RFC 8058 one-click compatible).

Routes are attached to the Flask app via ``register(app)``.
"""

from __future__ import annotations

import csv
import io
import json
import threading
import time
from typing import Any

from flask import (
    Response,
    abort,
    jsonify,
    request,
    send_file,
    url_for,
)

import db as bookdb
import email_sender


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _effective_settings(pub_id: str) -> dict[str, Any]:
    """Merge stored settings with sensible defaults (and default templates)."""
    saved = bookdb.get_review_settings(pub_id)
    if not saved.get("templates"):
        saved["templates"] = [dict(t) for t in email_sender.DEFAULT_TEMPLATES]
    if not saved.get("schedule_days"):
        saved["schedule_days"] = [t.get("offset_days", 7 * i) for i, t in enumerate(saved["templates"], 1)]
    return saved


def _build_context(
    pub: dict[str, Any],
    recipient: dict[str, Any],
    settings: dict[str, Any],
    *,
    base_url: str,
) -> dict[str, Any]:
    """Token map for template rendering."""
    mp = (recipient.get("marketplace") or "US").upper()
    mp_info = (pub.get("marketplaces") or {}).get(mp) or {}
    asin = mp_info.get("asin", "") or pub.get("primary_asin", "")
    url = mp_info.get("url", "")
    if not url and asin:
        domain = {
            "US": "amazon.com",
            "CA": "amazon.ca",
            "UK": "amazon.co.uk",
            "AU": "amazon.com.au",
        }.get(mp, "amazon.com")
        url = f"https://{domain}/dp/{asin}"
    # The dedicated review-write URL on Amazon — more direct than the product page.
    review_url = (
        f"https://{ {'US':'amazon.com','CA':'amazon.ca','UK':'amazon.co.uk','AU':'amazon.com.au'}.get(mp, 'amazon.com') }"
        f"/review/create-review?asin={asin}"
        if asin
        else url
    )
    unsubscribe_url = (
        base_url.rstrip("/")
        + "/u/"
        + (recipient.get("unsubscribe_token") or "")
    )
    return {
        "name": recipient.get("name") or "there",
        "email": recipient.get("email", ""),
        "title": pub.get("title", ""),
        "subtitle": pub.get("subtitle", ""),
        "author": settings.get("from_name", "") or "the author",
        "asin": asin,
        "marketplace": mp,
        "product_url": url,
        "review_url": review_url,
        "unsubscribe_url": unsubscribe_url,
    }


def _base_url_from_request() -> str:
    # Use the incoming request to build absolute URLs.
    return request.url_root.rstrip("/")


def _schedule_for_recipient(
    pub: dict[str, Any],
    recipient: dict[str, Any],
    settings: dict[str, Any],
    *,
    base_url: str,
) -> int:
    """Create scheduled email rows for every step in the sequence.

    Returns the number of new rows created (idempotent on (recipient_id, step)).
    """
    ctx = _build_context(pub, recipient, settings, base_url=base_url)
    trigger = float(recipient.get("trigger_date") or time.time())
    created = 0
    for tpl in settings["templates"]:
        step = int(tpl.get("step", 0) or 0)
        if step <= 0:
            continue
        offset_days = float(tpl.get("offset_days", step * 7))
        scheduled_for = trigger + offset_days * 86400.0
        subject = email_sender.render(tpl.get("subject", ""), ctx)
        body = email_sender.render(tpl.get("body", ""), ctx)
        sid = bookdb.schedule_email_send(
            publication_id=pub["id"],
            recipient_id=recipient["id"],
            step=step,
            scheduled_for=scheduled_for,
            subject=subject,
            body=body,
        )
        if sid:
            created += 1
    return created


def _reschedule_all(pub_id: str, *, base_url: str) -> int:
    """Rebuild the schedule for every pending recipient (used after settings change).

    Only schedules steps that don't already exist. Doesn't cancel sends that have
    already happened.
    """
    pub = bookdb.get_publication(pub_id)
    if not pub:
        return 0
    settings = _effective_settings(pub_id)
    n = 0
    for r in bookdb.list_review_recipients(pub_id, status="pending"):
        n += _schedule_for_recipient(pub, r, settings, base_url=base_url)
    return n


# ---------------------------------------------------------------------------
# Background SMTP tick (only runs when SMTP is configured)
# ---------------------------------------------------------------------------


_tick_thread: threading.Thread | None = None
_tick_stop = threading.Event()


def _tick_loop(interval_seconds: int = 60) -> None:
    while not _tick_stop.wait(interval_seconds):
        try:
            _process_due_smtp()
        except Exception:
            # Never let the loop die; failures are surfaced per-send.
            pass


def _process_due_smtp() -> int:
    """Send all due, scheduled emails via SMTP. Returns count sent."""
    cfg = email_sender.smtp_config()
    if cfg is None:
        return 0
    now = time.time()
    due = bookdb.list_email_sends(status="scheduled", due_before=now, limit=200)
    sent = 0
    for s in due:
        recipient = bookdb.get_review_recipient(s["recipient_id"])
        if not recipient or recipient.get("status") != "pending":
            bookdb.mark_email_send(s["id"], status="cancelled")
            continue
        # Build "From" — recipient-specific publication settings beat env default.
        settings = _effective_settings(s["publication_id"])
        from_addr = email_sender.format_from(
            settings.get("from_name", ""), settings.get("from_email", "")
        ) or cfg.default_from
        if not from_addr:
            bookdb.mark_email_send(
                s["id"], status="failed",
                error="No from_email configured for this publication or SMTP_FROM.",
            )
            continue
        # Unsubscribe URL is rebuilt absolute here because we have no request.
        # Fall back to the value already substituted into the body if APP_BASE_URL not set.
        import os
        base = os.getenv("APP_BASE_URL", "").rstrip("/")
        unsub = (base + "/u/" + recipient["unsubscribe_token"]) if base else ""
        try:
            msg = email_sender.build_message(
                from_addr=from_addr,
                to_addr=recipient["email"],
                subject=s["subject"],
                body=s["body"],
                unsubscribe_url=unsub,
            )
            email_sender.send_via_smtp(msg)
            bookdb.mark_email_send(s["id"], status="sent", method="smtp")
            sent += 1
        except Exception as exc:
            bookdb.mark_email_send(s["id"], status="failed", method="smtp", error=str(exc))
    return sent


def start_background_tick() -> None:
    # Auto-send is intentionally disabled: all review-ask emails stay at draft
    # level (pushed to MailerLite as drafts in a later pass). Do NOT spawn the
    # SMTP tick thread, even if SMTP is configured.
    return


# ---------------------------------------------------------------------------
# Flask registration
# ---------------------------------------------------------------------------


def register(app) -> None:  # noqa: ANN001
    # -- Settings --------------------------------------------------------
    @app.get("/api/publications/<pub_id>/review/settings")
    def get_settings(pub_id: str):  # noqa: ANN202
        if not bookdb.get_publication(pub_id):
            abort(404)
        return jsonify({
            "settings": _effective_settings(pub_id),
            "smtp_available": email_sender.smtp_available(),
            "summary": bookdb.review_summary(pub_id),
        })

    @app.post("/api/publications/<pub_id>/review/settings")
    def save_settings(pub_id: str):  # noqa: ANN202
        if not bookdb.get_publication(pub_id):
            abort(404)
        body = request.get_json(silent=True) or {}
        # Allow caller to pass templates with offset_days OR to pass schedule_days
        # separately. Merge them sensibly.
        templates = body.get("templates")
        schedule = body.get("schedule_days")
        if templates and not schedule:
            schedule = [int(t.get("offset_days", 7 * (i + 1))) for i, t in enumerate(templates)]
        bookdb.save_review_settings(
            pub_id,
            launch_date=body.get("launch_date"),
            from_name=(body.get("from_name") or "").strip(),
            from_email=(body.get("from_email") or "").strip(),
            schedule_days=schedule,
            templates=templates,
        )
        # Top up missing scheduled sends for already-enrolled recipients.
        _reschedule_all(pub_id, base_url=_base_url_from_request())
        return jsonify({"ok": True, "settings": _effective_settings(pub_id)})

    # -- Recipients ------------------------------------------------------
    @app.get("/api/publications/<pub_id>/review/recipients")
    def list_recipients(pub_id: str):  # noqa: ANN202
        if not bookdb.get_publication(pub_id):
            abort(404)
        status = request.args.get("status")
        recips = bookdb.list_review_recipients(pub_id, status=status)
        return jsonify({
            "recipients": recips,
            "summary": bookdb.review_summary(pub_id),
        })

    @app.post("/api/publications/<pub_id>/review/recipients")
    def add_recipient(pub_id: str):  # noqa: ANN202
        pub = bookdb.get_publication(pub_id)
        if not pub:
            abort(404)
        settings = _effective_settings(pub_id)
        body = request.get_json(silent=True) or {}
        email = (body.get("email") or "").strip().lower()
        if not email:
            return jsonify({"error": "email required"}), 400
        marketplace = (body.get("marketplace") or pub.get("primary_marketplace") or "US").upper()
        trigger_date = body.get("trigger_date")
        if trigger_date is None:
            trigger_date = float(settings.get("launch_date") or time.time())
        try:
            rid, created = bookdb.add_review_recipient(
                pub_id,
                email=email,
                name=(body.get("name") or "").strip(),
                marketplace=marketplace,
                trigger_date=float(trigger_date),
                source=(body.get("source") or "manual"),
                notes=(body.get("notes") or "").strip(),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if created:
            recip = bookdb.get_review_recipient(rid)
            _schedule_for_recipient(pub, recip, settings, base_url=_base_url_from_request())
        return jsonify({
            "id": rid,
            "created": created,
            "recipient": bookdb.get_review_recipient(rid),
            "summary": bookdb.review_summary(pub_id),
        })

    @app.post("/api/publications/<pub_id>/review/recipients/upload")
    def upload_recipients_csv(pub_id: str):  # noqa: ANN202
        pub = bookdb.get_publication(pub_id)
        if not pub:
            abort(404)
        settings = _effective_settings(pub_id)
        if "file" not in request.files:
            return jsonify({"error": "missing 'file' field"}), 400
        f = request.files["file"]
        try:
            text = f.read().decode("utf-8-sig", errors="replace")
        except Exception as exc:
            return jsonify({"error": f"bad file: {exc}"}), 400
        reader = csv.DictReader(io.StringIO(text))
        # Normalize header keys to lowercase
        added = 0
        skipped = 0
        errors: list[str] = []
        for i, row in enumerate(reader, start=2):  # row 1 is header
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            email = row.get("email") or ""
            if not email:
                continue
            name = row.get("name") or ""
            marketplace = (row.get("marketplace") or pub.get("primary_marketplace") or "US").upper()
            trigger_raw = row.get("trigger_date") or row.get("date") or ""
            trigger_date: float | None = None
            if trigger_raw:
                try:
                    # Accept ISO date / datetime
                    from datetime import datetime
                    trigger_date = datetime.fromisoformat(trigger_raw).timestamp()
                except Exception:
                    errors.append(f"row {i}: bad date {trigger_raw!r}")
                    trigger_date = None
            if trigger_date is None:
                trigger_date = float(settings.get("launch_date") or time.time())
            try:
                rid, created = bookdb.add_review_recipient(
                    pub_id,
                    email=email,
                    name=name,
                    marketplace=marketplace,
                    trigger_date=trigger_date,
                    source="csv",
                    notes=row.get("notes") or "",
                )
            except ValueError as exc:
                errors.append(f"row {i}: {exc}")
                continue
            if created:
                recip = bookdb.get_review_recipient(rid)
                _schedule_for_recipient(pub, recip, settings, base_url=_base_url_from_request())
                added += 1
            else:
                skipped += 1
        return jsonify({
            "added": added,
            "skipped_duplicates": skipped,
            "errors": errors,
            "summary": bookdb.review_summary(pub_id),
        })

    @app.post("/api/review/recipients/<recipient_id>/status")
    def set_recipient_status(recipient_id: str):  # noqa: ANN202
        body = request.get_json(silent=True) or {}
        status = (body.get("status") or "").lower()
        if status not in ("pending", "reviewed", "unsubscribed"):
            return jsonify({"error": "status must be pending|reviewed|unsubscribed"}), 400
        ok = bookdb.set_review_recipient_status(recipient_id, status)
        if not ok:
            abort(404)
        return jsonify({"ok": True, "recipient": bookdb.get_review_recipient(recipient_id)})

    @app.delete("/api/review/recipients/<recipient_id>")
    def del_recipient(recipient_id: str):  # noqa: ANN202
        ok = bookdb.delete_review_recipient(recipient_id)
        return jsonify({"ok": ok})

    # -- Email sends -----------------------------------------------------
    @app.get("/api/publications/<pub_id>/review/sends")
    def list_sends(pub_id: str):  # noqa: ANN202
        if not bookdb.get_publication(pub_id):
            abort(404)
        status = request.args.get("status")
        due_only = request.args.get("due_only") == "1"
        sends = bookdb.list_email_sends(
            pub_id,
            status=status,
            due_before=time.time() if due_only else None,
        )
        # Enrich with recipient info
        recips = {r["id"]: r for r in bookdb.list_review_recipients(pub_id)}
        for s in sends:
            r = recips.get(s["recipient_id"]) or {}
            s["recipient_email"] = r.get("email", "")
            s["recipient_name"] = r.get("name", "")
        return jsonify({
            "sends": sends,
            "summary": bookdb.review_summary(pub_id),
        })

    @app.get("/api/review/sends/<send_id>/preview")
    def preview_send(send_id: str):  # noqa: ANN202
        send = bookdb.get_email_send(send_id)
        if not send:
            abort(404)
        recipient = bookdb.get_review_recipient(send["recipient_id"])
        if not recipient:
            abort(404)
        settings = _effective_settings(send["publication_id"])
        from_addr = email_sender.format_from(
            settings.get("from_name", ""), settings.get("from_email", "")
        )
        return jsonify({
            "send": send,
            "recipient": recipient,
            "from": from_addr,
            "smtp_available": email_sender.smtp_available(),
        })

    @app.get("/api/review/sends/<send_id>/eml")
    def download_eml(send_id: str):  # noqa: ANN202
        send = bookdb.get_email_send(send_id)
        if not send:
            abort(404)
        recipient = bookdb.get_review_recipient(send["recipient_id"])
        pub = bookdb.get_publication(send["publication_id"])
        if not recipient or not pub:
            abort(404)
        settings = _effective_settings(send["publication_id"])
        from_addr = email_sender.format_from(
            settings.get("from_name", ""), settings.get("from_email", "")
        ) or "you@example.com"
        unsub = _base_url_from_request().rstrip("/") + "/u/" + (recipient["unsubscribe_token"] or "")
        msg = email_sender.build_message(
            from_addr=from_addr,
            to_addr=recipient["email"],
            subject=send["subject"],
            body=send["body"],
            unsubscribe_url=unsub,
        )
        data = email_sender.message_to_eml_bytes(msg)
        return send_file(
            io.BytesIO(data),
            mimetype="message/rfc822",
            as_attachment=True,
            download_name=f"review-{send['step']}-{recipient['email']}.eml",
        )

    @app.post("/api/review/sends/<send_id>/mark-sent")
    def mark_send_sent(send_id: str):  # noqa: ANN202
        if not bookdb.get_email_send(send_id):
            abort(404)
        method = (request.get_json(silent=True) or {}).get("method", "manual")
        bookdb.mark_email_send(send_id, status="sent", method=method)
        return jsonify({"ok": True})

    @app.post("/api/review/sends/<send_id>/cancel")
    def cancel_send(send_id: str):  # noqa: ANN202
        if not bookdb.get_email_send(send_id):
            abort(404)
        bookdb.mark_email_send(send_id, status="cancelled")
        return jsonify({"ok": True})

    @app.post("/api/review/sends/<send_id>/send-now")
    def send_now(send_id: str):  # noqa: ANN202
        """Force-send via SMTP. Requires SMTP configured."""
        send = bookdb.get_email_send(send_id)
        if not send:
            abort(404)
        if not email_sender.smtp_available():
            return jsonify({"error": "SMTP not configured (set SMTP_HOST in .env)"}), 400
        recipient = bookdb.get_review_recipient(send["recipient_id"])
        if not recipient or recipient.get("status") != "pending":
            return jsonify({"error": "recipient not eligible"}), 400
        settings = _effective_settings(send["publication_id"])
        cfg = email_sender.smtp_config()
        from_addr = email_sender.format_from(
            settings.get("from_name", ""), settings.get("from_email", "")
        ) or (cfg.default_from if cfg else "")
        if not from_addr:
            return jsonify({"error": "no from_email configured"}), 400
        unsub = _base_url_from_request().rstrip("/") + "/u/" + (recipient["unsubscribe_token"] or "")
        try:
            msg = email_sender.build_message(
                from_addr=from_addr,
                to_addr=recipient["email"],
                subject=send["subject"],
                body=send["body"],
                unsubscribe_url=unsub,
            )
            email_sender.send_via_smtp(msg)
            bookdb.mark_email_send(send_id, status="sent", method="smtp")
            return jsonify({"ok": True})
        except Exception as exc:
            bookdb.mark_email_send(send_id, status="failed", method="smtp", error=str(exc))
            return jsonify({"error": str(exc)}), 500

    @app.post("/api/publications/<pub_id>/review/process-smtp-tick")
    def process_smtp_tick(pub_id: str):  # noqa: ANN202
        """Manually trigger the SMTP send tick (also runs in background if enabled)."""
        if not email_sender.smtp_available():
            return jsonify({"error": "SMTP not configured"}), 400
        count = _process_due_smtp()
        return jsonify({"sent": count, "summary": bookdb.review_summary(pub_id)})

    # -- Public unsubscribe ---------------------------------------------
    @app.get("/u/<token>")
    @app.post("/u/<token>")
    def public_unsubscribe(token: str):  # noqa: ANN202
        recip = bookdb.find_recipient_by_unsubscribe_token(token)
        if not recip:
            return Response(
                "This unsubscribe link is invalid or expired.",
                status=404,
                mimetype="text/plain",
            )
        bookdb.set_review_recipient_status(recip["id"], "unsubscribed")
        # Plain HTML page; also returns 200 to satisfy RFC 8058 one-click.
        html = (
            "<!doctype html>"
            "<html><head><meta charset='utf-8'><title>Unsubscribed</title>"
            "<style>body{font-family:system-ui;text-align:center;padding:48px;color:#374151;}"
            "h1{color:#111827;}p{color:#6b7280;}</style></head>"
            "<body><h1>You're unsubscribed.</h1>"
            "<p>You won't receive any more review-request emails for this book.</p>"
            "</body></html>"
        )
        return Response(html, status=200, mimetype="text/html")

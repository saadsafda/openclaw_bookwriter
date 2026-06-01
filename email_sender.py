"""Email rendering + sending for review-request automation.

Two send modes, both supported simultaneously:

* **manual** (default, zero credentials): we render the email and return a
  ready-to-paste subject/body or a downloadable ``.eml`` file. You send it via
  whatever tool you already use, then mark it sent.

* **smtp** (optional): if the ``SMTP_HOST`` env var is set, sends are dispatched
  in-process via stdlib ``smtplib``.

Templates use simple ``{{token}}`` substitution. The full token set is
documented in ``DEFAULT_TEMPLATES`` below.
"""

from __future__ import annotations

import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass


# ---------------------------------------------------------------------------
# Token rendering
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def render(template: str, ctx: dict[str, Any]) -> str:
    """Substitute ``{{token}}`` placeholders. Missing tokens become ''."""

    def repl(match: re.Match) -> str:
        key = match.group(1)
        return str(ctx.get(key, ""))

    return _TOKEN_RE.sub(repl, template or "")


# ---------------------------------------------------------------------------
# Default 3-step sequence
# ---------------------------------------------------------------------------

DEFAULT_TEMPLATES: list[dict[str, Any]] = [
    {
        "step": 1,
        "offset_days": 7,
        "subject": "How's {{title}} treating you, {{name}}?",
        "body": (
            "Hi {{name}},\n\n"
            "I hope you've been enjoying {{title}}. I'd love to hear your "
            "honest thoughts.\n\n"
            "If you've had a chance to read it, would you mind leaving a "
            "short review on Amazon? Reviews help other readers decide if "
            "the book is right for them and make a huge difference for "
            "independent authors like me.\n\n"
            "Here's the direct review link:\n{{review_url}}\n\n"
            "Thanks so much,\n{{author}}\n\n"
            "---\n"
            "Don't want any more emails about this book? "
            "Unsubscribe: {{unsubscribe_url}}\n"
        ),
    },
    {
        "step": 2,
        "offset_days": 14,
        "subject": "Quick reminder about {{title}}",
        "body": (
            "Hi {{name}},\n\n"
            "Just a quick nudge — if you've finished {{title}}, a short "
            "review on Amazon would mean the world.\n\n"
            "Even one or two sentences is plenty:\n{{review_url}}\n\n"
            "Thanks!\n{{author}}\n\n"
            "---\n"
            "Unsubscribe: {{unsubscribe_url}}\n"
        ),
    },
    {
        "step": 3,
        "offset_days": 30,
        "subject": "Last ask: a review for {{title}}?",
        "body": (
            "Hi {{name}},\n\n"
            "Last note from me about {{title}}, I promise.\n\n"
            "If the book helped (or didn't), an honest Amazon review is "
            "the single best way to support what I do.\n\n"
            "{{review_url}}\n\n"
            "Either way, thanks for reading.\n{{author}}\n\n"
            "---\n"
            "Unsubscribe: {{unsubscribe_url}}\n"
        ),
    },
]


# ---------------------------------------------------------------------------
# SMTP config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SMTPConfig:
    host: str
    port: int
    user: str
    password: str
    use_tls: bool  # STARTTLS on submission port (587)
    use_ssl: bool  # implicit TLS (465)
    default_from: str  # "Name <email@host>"


def smtp_config() -> SMTPConfig | None:
    """Build SMTPConfig from env, or return None if not configured."""
    host = (os.getenv("SMTP_HOST") or "").strip()
    if not host:
        return None
    port = int(os.getenv("SMTP_PORT") or "587")
    user = os.getenv("SMTP_USER") or ""
    password = os.getenv("SMTP_PASS") or ""
    mode = (os.getenv("SMTP_MODE") or "starttls").lower()
    use_ssl = mode == "ssl" or port == 465
    use_tls = mode == "starttls" or (port == 587 and not use_ssl)
    default_from = (os.getenv("SMTP_FROM") or "").strip()
    return SMTPConfig(
        host=host, port=port, user=user, password=password,
        use_tls=use_tls, use_ssl=use_ssl, default_from=default_from,
    )


def smtp_available() -> bool:
    return smtp_config() is not None


# ---------------------------------------------------------------------------
# Building messages
# ---------------------------------------------------------------------------


def build_message(
    *,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    unsubscribe_url: str = "",
    reply_to: str = "",
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject or "(no subject)"
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Message-ID"] = make_msgid()
    # RFC 8058 / RFC 2369 one-click unsubscribe
    if unsubscribe_url:
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(body or "")
    return msg


def message_to_eml_bytes(msg: EmailMessage) -> bytes:
    return bytes(msg)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def send_via_smtp(msg: EmailMessage) -> None:
    """Send a built EmailMessage via configured SMTP. Raises on failure."""
    cfg = smtp_config()
    if cfg is None:
        raise RuntimeError("SMTP is not configured. Set SMTP_HOST in .env to enable.")

    context = ssl.create_default_context()
    if cfg.use_ssl:
        with smtplib.SMTP_SSL(cfg.host, cfg.port, context=context, timeout=30) as s:
            if cfg.user:
                s.login(cfg.user, cfg.password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg.host, cfg.port, timeout=30) as s:
            s.ehlo()
            if cfg.use_tls:
                s.starttls(context=context)
                s.ehlo()
            if cfg.user:
                s.login(cfg.user, cfg.password)
            s.send_message(msg)


# ---------------------------------------------------------------------------
# Convenience: format a "From:" header
# ---------------------------------------------------------------------------


def format_from(name: str, email: str) -> str:
    name = (name or "").strip()
    email = (email or "").strip()
    if not email:
        return ""
    return formataddr((name, email)) if name else email

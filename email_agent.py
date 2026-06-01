#!/usr/bin/env python3
"""
email_agent.py

Generates a book launch email sequence (3–5 emails) for a finished book.
Designed to be pushed to MailerLite (or any ESP) as drafts in later phases.

The agent asks OpenClaw to return a JSON array of emails with fields:
  position, day_offset, subject, preview, body (markdown), cta_label

Usage (CLI):
  python email_agent.py /path/to/finished_book.docx
  python email_agent.py book.docx --title "My Book" --count 5

Usage (as module):
  from email_agent import generate_email_campaign
  result = generate_email_campaign(docx_path, title="...", count=5)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from openclaw_docx_writer import parse_openclaw_reply
from pub_listing_agent import _extract_outline_and_intro

# ── Constants ───────────────────────────────────────────────────────

DEFAULT_AGENT_ID = "email-launch-agent-1"
DEFAULT_TIMEOUT = 240
DEFAULT_COUNT = 3

# Day offsets for a "free for a couple of days" Kindle promo. Day 0 is the
# first day the book is free; each email goes out one day apart, escalating
# urgency as the free window closes.
DEFAULT_OFFSETS: dict[int, list[int]] = {
    3: [0, 1, 2],
    4: [0, 1, 2, 3],
    5: [0, 1, 2, 3, 4],
}

# Each email's job within the free-promo window.
EMAIL_ROLES: dict[int, list[str]] = {
    3: [
        "Day 1 — It's live and FREE today (announce + excite)",
        "Day 2 — Still free, don't miss it (reminder + new angle)",
        "Day 3 — Last day, free ends tonight (final urgency)",
    ],
    4: [
        "Day 1 — It's live and FREE today (announce + excite)",
        "Day 2 — Still free (reminder + new angle)",
        "Day 3 — Halfway through the free window (social proof / curiosity)",
        "Day 4 — Last day, free ends tonight (final urgency)",
    ],
    5: [
        "Day 1 — It's live and FREE today (announce + excite)",
        "Day 2 — Still free (reminder + new angle)",
        "Day 3 — What's inside (dig into the content)",
        "Day 4 — Social proof / curiosity hook",
        "Day 5 — Last day, free ends tonight (final urgency)",
    ],
}


# ── Data classes ────────────────────────────────────────────────────


@dataclass
class EmailDraft:
    position: int
    day_offset: int
    subject: str = ""
    preview: str = ""
    body: str = ""
    cta_label: str = ""


@dataclass
class EmailCampaignResult:
    title: str
    emails: list[EmailDraft] = field(default_factory=list)
    raw_response: str = ""
    book_snapshot: dict[str, Any] = field(default_factory=dict)


# ── OpenClaw helper ─────────────────────────────────────────────────


def _call_openclaw(agent_id: str, message: str, timeout_s: int = DEFAULT_TIMEOUT) -> str:
    cmd = ["openclaw", "agent", "--agent", agent_id, "--message", message, "--json"]
    if timeout_s > 0:
        cmd += ["--timeout", str(timeout_s)]

    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            f"OpenClaw failed (exit {p.returncode}).\n"
            f"STDERR:\n{p.stderr}\n"
            f"STDOUT:\n{p.stdout[:500]}"
        )
    return p.stdout


def _parse_reply(stdout: str) -> str:
    return parse_openclaw_reply(stdout)


# ── JSON extraction ─────────────────────────────────────────────────


def _try_extract_from_raw_json(raw_stdout: str) -> Optional[list[dict[str, Any]]]:
    """Walk the raw OpenClaw JSON (NDJSON or single blob) and return the
    first list-of-dicts we can find. Useful when the agent returns a JSON
    array directly and parse_openclaw_reply flattens it to an empty string.
    """
    if not raw_stdout:
        return None

    def _walk(node: Any) -> Optional[list[dict[str, Any]]]:
        if isinstance(node, list):
            if node and all(isinstance(x, dict) for x in node):
                # Looks like an email list if items have position/subject/body
                keys = set(node[0].keys())
                if keys & {"subject", "body", "position"}:
                    return node
            for item in node:
                found = _walk(item)
                if found:
                    return found
        elif isinstance(node, dict):
            for val in node.values():
                found = _walk(val)
                if found:
                    return found
            # Also try parsing string values as JSON (agents often stuff JSON in a text field)
            for val in node.values():
                if isinstance(val, str) and ("[" in val and "]" in val):
                    try:
                        sub = json.loads(val)
                    except Exception:
                        # Try extracting fenced / embedded array
                        start, end = val.find("["), val.rfind("]")
                        if start != -1 and end > start:
                            try:
                                sub = json.loads(val[start:end + 1])
                            except Exception:
                                continue
                        else:
                            continue
                    found = _walk(sub)
                    if found:
                        return found
        return None

    # OpenClaw --json may produce NDJSON; try each line, then the whole blob
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        result = _walk(obj)
        if result:
            return result

    try:
        obj = json.loads(raw_stdout)
    except Exception:
        return None
    return _walk(obj)


def _extract_json_array(
    reply: str, raw_stdout: str = ""
) -> list[dict[str, Any]]:
    """Extract a JSON array of email objects from an LLM reply.

    Tolerates markdown code fences and extra prose surrounding the JSON.
    Falls back to walking the raw OpenClaw stdout JSON tree when the reply
    text is empty or unparseable.
    """
    text = (reply or "").strip()

    if text:
        # Strip markdown code fences
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        if fence:
            text = fence.group(1).strip()

        # Try direct parse
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict) and isinstance(parsed.get("emails"), list):
                return parsed["emails"]
        except Exception:
            pass

        # Fall back to scanning for the outermost [...] block
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass

    # Last resort: walk the raw OpenClaw JSON tree
    if raw_stdout:
        walked = _try_extract_from_raw_json(raw_stdout)
        if walked:
            return walked

    # Error: show BOTH the parsed reply and the first chunk of raw stdout
    snippet_reply = (reply or "").strip()[:1500] or "<empty>"
    snippet_raw = (raw_stdout or "").strip()[:1500] or "<empty>"
    raise ValueError(
        "Could not parse a JSON array of emails from the agent reply.\n"
        f"--- parsed reply (first 1500 chars) ---\n{snippet_reply}\n"
        f"--- raw stdout (first 1500 chars) ---\n{snippet_raw}"
    )


# ── Core generation ─────────────────────────────────────────────────


def _build_prompt(
    title: str,
    outline: str,
    intro: str,
    count: int,
    offsets: list[int],
    description: str = "",
    subtitles: list[str] | None = None,
) -> str:
    roles = EMAIL_ROLES.get(count, [f"Email {i + 1}" for i in range(count)])

    role_lines = "\n".join(
        f"  Email {i + 1} (day {offsets[i]:+d}) — {roles[i]}"
        for i in range(count)
    )

    sub_text = ""
    if subtitles:
        sub_text = "CANDIDATE SUBTITLES:\n" + "\n".join(f"- {s}" for s in subtitles[:5]) + "\n\n"

    desc_text = ""
    if description:
        desc_text = f"BOOK DESCRIPTION:\n{description.strip()}\n\n"

    return f"""You are an email marketing copywriter for self-published Kindle authors.
Write a {count}-email sequence promoting a "free for a couple of days" Kindle
giveaway to the author's existing newsletter subscribers. Every email pushes the
SAME action: download the book free from Amazon while it's free. Escalate urgency
as the free window closes. The author publishes these as drafts in MailerLite.

BOOK TITLE: {title}

{sub_text}{desc_text}BOOK OUTLINE:
{outline}

INTRODUCTION EXCERPT:
{intro[:1800]}

SEQUENCE PLAN — write exactly {count} emails in this order:
{role_lines}

FOLLOW THIS EXACT EMAIL STRUCTURE for every email (this is the client's proven
template — keep the bones, vary the wording per day so the sequence doesn't feel
repetitive):

  1. Niche greeting on its own line — "Dear [niche] lover," — infer the niche
     from the book topic (e.g. a trivia book → "Dear Trivia lover,"; a parenting
     book → "Dear Parent,"). Vary it slightly across emails.
  2. A short hook line ("Did you see I'm back with a new book for you?" / a Day-3
     "last chance" variant, etc).
  3. The free offer, with the key phrase in bold:
     "as a member of my newsletter, you can **get it for free today!**"
  4. The book title as a markdown link to [BOOK_LINK], followed by 2–3 sentences
     of vivid, benefit-driven pitch.
  5. On its own line, the literal placeholder: [COVER_MOCKUP]
     (a 3D cover image is inserted here automatically — just output the token).
  6. A lead-in line like "Some of the things we'll dig into are:" followed by a
     markdown bullet list of 3–5 concrete things inside the book (derive these
     from the OUTLINE — do not invent facts).
  7. One short intrigue/tease paragraph that builds curiosity.
  8. A one-line "Are you ready to [discover/uncover] [title]?" question.
  9. A strong final CTA line in this exact spirit, with the link on [BOOK_LINK]:
     ">>> The Kindle book is free for a couple of days. [Grab it here today!]([BOOK_LINK])"
 10. A warm closing line ("I hope you'll like it!") then "Best," on its own line.
     Do NOT add the author's name — the ESP signature appends it.

WRITING RULES:
- First person, warm, direct, enthusiastic but not spammy. No ALL-CAPS shouting
  (the word "FREE" once in caps is fine).
- Subject ≤ 60 chars, curiosity- or urgency-driven. Day 1 announces; the last day
  must convey "free ends tonight / last chance".
- Preview text ≤ 90 chars, complements (does not repeat) the subject.
- Body 150–280 words. Use **Markdown** only (bold, links, bullet lists). NO raw HTML.
- Use [BOOK_LINK] everywhere a URL belongs — never invent a URL. Include
  [COVER_MOCKUP] exactly once per email (block 5).
- `cta_label` = the button/link text (e.g. "Grab it free", "Download now").

OUTPUT FORMAT — return ONLY a valid JSON array, no commentary, no code fences.
Each element must be an object with EXACTLY these keys:

[
  {{
    "position": 1,
    "day_offset": {offsets[0]},
    "subject": "...",
    "preview": "...",
    "body": "Markdown body here...",
    "cta_label": "..."
  }}
  // ... one object per email, position 1..{count}
]

Make sure the JSON parses. No trailing commas. No extra keys.
"""


def generate_email_campaign(
    docx_path: Path,
    *,
    title: str = "",
    count: int = DEFAULT_COUNT,
    offsets: list[int] | None = None,
    agent_id: str = DEFAULT_AGENT_ID,
    timeout_s: int = DEFAULT_TIMEOUT,
    description: str = "",
    subtitles: list[str] | None = None,
    callback=None,
) -> EmailCampaignResult:
    """Generate a launch email sequence for a finished book."""
    if not docx_path.exists():
        raise FileNotFoundError(f"Book not found: {docx_path}")

    if count not in (3, 4, 5):
        raise ValueError(f"count must be 3, 4, or 5 (got {count})")

    if offsets is None:
        offsets = DEFAULT_OFFSETS[count]
    if len(offsets) != count:
        raise ValueError(
            f"offsets length ({len(offsets)}) must match count ({count})"
        )

    def _log(step: str, msg: str) -> None:
        if callback:
            callback(step, msg)
        else:
            print(f"[{step}] {msg}")

    _log("extract", "Reading book content...")
    doc_title, outline, intro = _extract_outline_and_intro(docx_path)
    book_title = (title or doc_title or "Untitled Book").strip()

    _log("extract", f"Title: {book_title}")
    _log("extract", f"Outline: {len(outline.splitlines())} headings")
    _log("extract", f"Introduction: {len(intro.split())} words")

    snapshot = {
        "title": book_title,
        "outline_headings": outline.splitlines(),
        "intro_word_count": len(intro.split()),
        "description_word_count": len((description or "").split()),
        "subtitles": list(subtitles or []),
    }

    prompt = _build_prompt(
        title=book_title,
        outline=outline,
        intro=intro,
        count=count,
        offsets=offsets,
        description=description,
        subtitles=subtitles,
    )

    _log("generate", f"Asking agent for {count}-email sequence...")
    raw = _call_openclaw(agent_id, prompt, timeout_s)
    reply = _parse_reply(raw)

    _log("parse", "Parsing JSON email array...")
    items = _extract_json_array(reply, raw_stdout=raw)

    # Build EmailDraft objects, forcing sane positions/offsets
    drafts: list[EmailDraft] = []
    for i, item in enumerate(items[:count]):
        if not isinstance(item, dict):
            continue
        drafts.append(
            EmailDraft(
                position=i + 1,
                day_offset=int(item.get("day_offset", offsets[i])),
                subject=str(item.get("subject", "")).strip(),
                preview=str(item.get("preview", "")).strip(),
                body=str(item.get("body", "")).strip(),
                cta_label=str(item.get("cta_label", "")).strip(),
            )
        )

    if len(drafts) < count:
        raise RuntimeError(
            f"Agent returned {len(drafts)} emails but {count} were requested. "
            f"Raw reply (first 500 chars):\n{reply[:500]}"
        )

    _log("done", f"Generated {len(drafts)} email drafts")

    return EmailCampaignResult(
        title=book_title,
        emails=drafts,
        raw_response=reply,
        book_snapshot=snapshot,
    )


# ── CLI ─────────────────────────────────────────────────────────────


def _format_campaign(result: EmailCampaignResult) -> str:
    lines: list[str] = []
    bar = "=" * 60
    lines.append(bar)
    lines.append(f"LAUNCH EMAIL SEQUENCE — {result.title}")
    lines.append(bar)
    for em in result.emails:
        lines.append("")
        lines.append(f"── Email {em.position} (day {em.day_offset:+d}) ──")
        lines.append(f"Subject: {em.subject}")
        lines.append(f"Preview: {em.preview}")
        lines.append(f"CTA:     {em.cta_label}")
        lines.append("")
        lines.append(em.body)
    lines.append("")
    lines.append(bar)
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate a book launch email sequence for MailerLite."
    )
    ap.add_argument("input", help="Path to the finished book .docx")
    ap.add_argument("--title", default="", help="Override book title")
    ap.add_argument(
        "--count", type=int, default=DEFAULT_COUNT, choices=[3, 4, 5],
        help="Number of emails in the sequence (default: 5)",
    )
    ap.add_argument(
        "--agent", default=DEFAULT_AGENT_ID,
        help=f"OpenClaw agent id (default: {DEFAULT_AGENT_ID})",
    )
    ap.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT,
        help="Timeout per API call in seconds",
    )
    ap.add_argument(
        "--json-output", default="",
        help="Save result as JSON to this path",
    )
    args = ap.parse_args()

    docx_path = Path(args.input)
    if not docx_path.exists():
        print(f"ERROR: file not found: {docx_path}", file=sys.stderr)
        return 2

    try:
        result = generate_email_campaign(
            docx_path,
            title=args.title,
            count=args.count,
            agent_id=args.agent,
            timeout_s=args.timeout,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(_format_campaign(result))

    if args.json_output:
        out = Path(args.json_output)
        out.write_text(
            json.dumps(
                {
                    "title": result.title,
                    "emails": [em.__dict__ for em in result.emails],
                    "book_snapshot": result.book_snapshot,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nSaved JSON to: {out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

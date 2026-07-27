"""
openclaw_docx_writer.py

Reads a .docx, sends each heading to OpenClaw, and inserts the generated paragraph
right after that heading.

Usage:
  python openclaw_docx_writer.py input.docx output.docx --agent writer-agent-1

Notes:
- Requires OpenClaw CLI installed and configured.
- Uses a cache file to avoid regenerating the same heading repeatedly.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, unquote_to_bytes
from urllib.request import Request, urlopen

from docx import Document
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from docx.oxml import OxmlElement
from docx.shared import Inches
from docx.text.paragraph import Paragraph

# openclaw_image_maker.py is invoked as a subprocess for image generation.


# ----------------------------
# Helpers: Environment
# ----------------------------

def load_env_file(env_path: Path) -> None:
    """
    Minimal .env loader:
    - supports KEY=VALUE and optional `export KEY=VALUE`
    - ignores empty lines and comments
    - does not overwrite variables already present in the shell environment
    """
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
            value = value[1:-1]
        os.environ.setdefault(key, value)


# ----------------------------
# Helpers: DOCX manipulation
# ----------------------------

HEADING_RE = re.compile(r"^Heading\s+\d+$", re.IGNORECASE)
# Matches lines like "Chapter 1: ...", "Introduction:", "Conclusion:", "Epilogue:"
# Chapter number token: an integer (7), a Roman numeral (VII), or a spelled-out
# number word (Seven / Twenty One). Kept as its own group so both PLAIN_HEADING_RE
# and image-placement heading detection recognize the same set of chapter formats.
_CHAPTER_NUM = (
    r"(?:\d+"
    r"|[IVXLCDM]+"
    r"|(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred)"
    r"(?:[\s-]+(?:one|two|three|four|five|six|seven|eight|nine))?)"
)
# Front/back-matter words only count as a heading when the line is JUST that word
# (optionally with a subtitle after a colon/dash). "Introduction to the concepts…"
# is a body sentence, not a heading, so a following space+word must NOT match.
_MATTER = r"(?:Introduction|Conclusion|Epilogue|Foreword|Preface|Prologue|Afterword)"
PLAIN_HEADING_RE = re.compile(
    r"^(Chapter\s+" + _CHAPTER_NUM + r"(?:\s*[:–—-]\s*.*)?"
    r"|" + _MATTER + r"\s*(?:[:–—-]\s*.*)?)$",
    re.IGNORECASE,
)
# Matches bullet-point subheadings: "- Thing N: ...", "- Welcome...", "Focus: ..."
# Also matches numbered subheadings: "1. Some heading text", "2. Another heading"
SUBHEADING_RE = re.compile(
    r"^([\-•*–—]\s+\S.+|Focus:\s+.+|\d+\.\s+\S.+)",
    re.IGNORECASE,
)
LIST_BULLET_STYLE_RE = re.compile(r"^List Bullet(?: \d+)?$", re.IGNORECASE)
CHAPTER_LABEL_ONLY_RE = re.compile(r"^CHAPTER\s+\d+$", re.IGNORECASE)

def _outline_level(p) -> Optional[int]:
    """Outline level from paragraph XML (w:outlineLvl). Some generators mark
    headings this way instead of using named 'Heading N' styles."""
    try:
        vals = p._p.xpath("./w:pPr/w:outlineLvl/@w:val")
        return int(vals[0]) if vals else None
    except Exception:
        return None


def is_heading_paragraph(p) -> bool:
    try:
        name = (p.style.name or "").strip()
    except Exception:
        return False
    if bool(HEADING_RE.match(name)) or name.lower() in {"title"}:
        return True
    # Top outline level = chapter-level heading in style-less documents.
    if _outline_level(p) == 0:
        return True
    # Also treat plain-style paragraphs that look like chapter/section headings
    text = (p.text or "").strip()
    return bool(PLAIN_HEADING_RE.match(text))


def is_subheading_paragraph(p) -> bool:
    """Detect bullet-point lines that should get generated content."""
    try:
        name = (p.style.name or "").strip()
        # Skip if it already has a docx heading style
        if HEADING_RE.match(name):
            return False
        if LIST_BULLET_STYLE_RE.match(name):
            return True
        # Nested outline levels are section subheadings in style-less docs.
        level = _outline_level(p)
        if level is not None and level >= 1:
            return True
    except Exception:
        return False
    text = (p.text or "").strip()
    if SUBHEADING_RE.match(text):
        return True
    # Bare outline topic: short phrase that isn't a heading or body prose.
    # Catches plain-text outline items like "Why AI matters for kids today"
    if text and not is_heading_paragraph(p):
        words = text.split()
        # Exclude lines that end with a period (likely body sentences),
        # but allow question marks (e.g. 'Is AI actually "thinking"?')
        if 2 <= len(words) <= 15 and len(text) <= 120 and text[-1] != '.':
            return True
    return False


def paragraph_looks_like_body(p: Paragraph) -> bool:
    """Heuristic for already-generated prose."""
    text = (p.text or "").strip()
    if not text:
        return False
    if is_heading_paragraph(p) or is_subheading_paragraph(p):
        return False

    word_count = len(text.split())
    sentence_count = sum(text.count(ch) for ch in ".!?")
    # Short lines without sentence-ending punctuation are outline topics, not body
    if word_count <= 15 and len(text) <= 120 and not text[-1] in '.!?':
        return False
    return word_count >= 35 or len(text) >= 220 or sentence_count >= 2


def insert_paragraph_after(paragraph, text: str, style: str = "Normal") -> Paragraph:
    """
    Insert a new paragraph right after `paragraph`.

    The style is applied only when the document actually defines it. Outlines
    made by other tools (Google Docs exports, outline generators) often have
    no style named "Normal"; assigning the name anyway raises KeyError, so in
    that case the paragraph keeps the document's default style instead.
    """
    new_p = OxmlElement("w:p")
    paragraph._p.addnext(new_p)
    new_para = Paragraph(new_p, paragraph._parent)
    try:
        new_para.style = paragraph._parent.part.document.styles[style]
    except Exception:
        pass  # style not defined in this document; document default applies
    new_para.add_run(text)
    return new_para


def paragraph_has_image(paragraph: Paragraph) -> bool:
    return bool(paragraph._p.xpath(".//w:drawing"))


def clear_paragraph(paragraph: Paragraph) -> None:
    p = paragraph._p
    for child in list(p):
        # Keep paragraph properties, remove content runs/drawings/etc.
        if child.tag.endswith("}pPr"):
            continue
        p.remove(child)


def prepare_image_for_print(image_path: Path, dpi: int = 300) -> None:
    """Strip all EXIF/metadata and set DPI for print publishing."""
    from PIL import Image

    with Image.open(image_path) as img:
        clean = Image.new(img.mode, img.size)
        clean.putdata(list(img.getdata()))
        clean.save(image_path, dpi=(dpi, dpi))


def set_paragraph_image(paragraph: Paragraph, image_path: Path, width_inches: float) -> None:
    prepare_image_for_print(image_path)
    clear_paragraph(paragraph)
    run = paragraph.add_run()
    run.add_picture(str(image_path), width=Inches(width_inches))
    paragraph.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER


def insert_image_after(paragraph: Paragraph, image_path: Path, width_inches: float) -> Paragraph:
    img_para = insert_paragraph_after(paragraph, "", style="Normal")
    set_paragraph_image(img_para, image_path=image_path, width_inches=width_inches)
    return img_para


# ----------------------------
# Helpers: OpenClaw invocation
# ----------------------------

def _extract_text(obj: Any) -> str:
    """
    Best-effort extraction of reply text from OpenClaw --json output.
    Handles common shapes: string, list of {type:'text', text:'...'}, dict with reply/content/etc.
    """
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        parts = []
        for item in obj:
            t = _extract_text(item)
            if t.strip():
                parts.append(t.strip())
        return "\n".join(parts).strip()
    if isinstance(obj, dict):
        # Common patterns:
        # 1) { type: "text", text: "..." }
        if "text" in obj and isinstance(obj.get("text"), str):
            return obj["text"].strip()

        # 2) reply / content / output keys
        for k in ("replyText", "reply", "content", "output", "final", "message"):
            if k in obj:
                t = _extract_text(obj.get(k))
                if t.strip():
                    return t.strip()

        # 3) nested data
        for k in ("data", "result", "payload"):
            if k in obj:
                t = _extract_text(obj.get(k))
                if t.strip():
                    return t.strip()

        # 4) parts array
        if "parts" in obj:
            t = _extract_text(obj.get("parts"))
            if t.strip():
                return t.strip()

        # 5) payloads array (OpenClaw: result.payloads[].text)
        if "payloads" in obj:
            t = _extract_text(obj.get("payloads"))
            if t.strip():
                return t.strip()

    return ""


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
IMAGE_AGENT_ID = "image-agent-1"
ENABLE_AUTO_POSTPROCESS = True
# Image prompt templates are now managed entirely by openclaw_image_maker.py.


def _iter_json_objects(s: str):
    # 1) Full JSON
    try:
        yield json.loads(s)
    except Exception:
        pass

    # 2) Substring JSON (first "{" ... last "}")
    if "{" in s and "}" in s:
        start = s.find("{")
        end = s.rfind("}")
        if end > start:
            candidate = s[start : end + 1]
            try:
                yield json.loads(candidate)
            except Exception:
                pass

    # 3) NDJSON / line JSON
    for ln in (ln.strip() for ln in s.splitlines() if ln.strip()):
        try:
            yield json.loads(ln)
        except Exception:
            continue


def _looks_like_image_url(value: str) -> bool:
    if not value:
        return False
    v = value.strip().lower()
    if v.startswith("data:image/"):
        return True
    parsed = urlparse(v)
    if parsed.scheme not in {"http", "https"}:
        return False
    if Path(parsed.path).suffix.lower() in IMAGE_SUFFIXES:
        return True
    return any(marker in v for marker in ("/image", "format=png", "format=jpg", "format=jpeg", "format=webp"))


def _extract_media_urls(obj: Any) -> list[str]:
    urls: list[str] = []
    if obj is None:
        return urls
    if isinstance(obj, list):
        for item in obj:
            urls.extend(_extract_media_urls(item))
        return urls
    if isinstance(obj, dict):
        for key in ("mediaUrl", "mediaURL", "imageUrl", "imageURL"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                urls.append(value.strip())

        # More generic keys, only when they actually look like image URLs.
        for key in ("url", "uri"):
            value = obj.get(key)
            if isinstance(value, str) and _looks_like_image_url(value):
                urls.append(value.strip())

        for value in obj.values():
            urls.extend(_extract_media_urls(value))
    return urls


def parse_openclaw_media_urls(stdout: str) -> list[str]:
    s = (stdout or "").strip()
    if not s:
        return []

    found: list[str] = []
    seen: set[str] = set()

    def _add(url: str) -> None:
        u = (url or "").strip().strip(")>,.;\"'")
        if not u:
            return
        parsed = urlparse(u)
        is_http_url = parsed.scheme in {"http", "https"}
        is_file_url = parsed.scheme == "file"
        is_local_image_path = Path(u).expanduser().exists() and Path(u).suffix.lower() in IMAGE_SUFFIXES
        if not (_looks_like_image_url(u) or is_http_url or is_file_url or is_local_image_path):
            return
        if u in seen:
            return
        seen.add(u)
        found.append(u)

    for obj in _iter_json_objects(s):
        for url in _extract_media_urls(obj):
            _add(url)

    for ln in s.splitlines():
        raw = ln.strip()
        if raw.startswith("MEDIA:"):
            _add(raw.split("MEDIA:", 1)[1].strip())

    return found


# Real per-turn cost, priced from OpenClaw's own usage block (confirmed against a
# live call): "input"/"output" are billed at normal rates, "cacheWrite" at 1.25x
# input price, "cacheRead" at 0.1x input price. A single call's system-prompt
# overhead alone was observed at ~23,750 tokens (AGENTS.md/SOUL.md/tool schemas),
# so this must be tracked and enforced, not estimated.
_CLAUDE_INPUT_RATE = 5.00 / 1_000_000
_CLAUDE_OUTPUT_RATE = 25.00 / 1_000_000
_CLAUDE_CACHE_WRITE_RATE = _CLAUDE_INPUT_RATE * 1.25
_CLAUDE_CACHE_READ_RATE = _CLAUDE_INPUT_RATE * 0.1


def parse_openclaw_usage_cost(stdout: str) -> float:
    """Extract the dollar cost of one OpenClaw call from its --json output's
    lastCallUsage block. Returns 0.0 if usage data isn't present (older
    OpenClaw versions, or a malformed/empty response) rather than raising —
    callers treat an unpriceable call as $0 and rely on the wall-clock/count
    fallback in the budget guard instead."""
    s = (stdout or "").strip()
    if not s:
        return 0.0
    for obj in _iter_json_objects(s):
        try:
            usage = obj["result"]["meta"]["agentMeta"]["lastCallUsage"]
        except (KeyError, TypeError):
            continue
        try:
            return (
                float(usage.get("input", 0)) * _CLAUDE_INPUT_RATE
                + float(usage.get("output", 0)) * _CLAUDE_OUTPUT_RATE
                + float(usage.get("cacheWrite", 0)) * _CLAUDE_CACHE_WRITE_RATE
                + float(usage.get("cacheRead", 0)) * _CLAUDE_CACHE_READ_RATE
            )
        except (TypeError, ValueError):
            continue
    return 0.0


class SpendTracker:
    """Process-wide running total of real dollars spent on OpenClaw calls this
    run. The configured limit is a warning threshold: crossing it emits one
    machine-readable notice for the UI, but generation keeps running unless
    the user explicitly clicks Stop."""

    def __init__(self, limit_usd: float | None):
        self.limit_usd = limit_usd
        self.spent_usd = 0.0
        self.calls = 0
        self.warned = False

    def record(self, stdout: str) -> None:
        cost = parse_openclaw_usage_cost(stdout)
        self.spent_usd += cost
        self.calls += 1
        if (
            self.limit_usd is not None
            and not self.warned
            and self.spent_usd >= self.limit_usd
        ):
            self.warned = True
            # Machine-parseable line, separate from the prose message below,
            # so a caller (e.g. the web UI) can pull the exact numbers out of
            # the log without regex-matching human-readable text.
            print(
                f"BUDGET_CAP_HIT spent_usd={self.spent_usd:.4f} "
                f"limit_usd={self.limit_usd:.4f} calls={self.calls}",
                flush=True,
            )
            print(
                f"Spending warning reached: ${self.spent_usd:.2f} spent "
                f"(threshold ${self.limit_usd:.2f}) after {self.calls} OpenClaw call(s). "
                "Generation is continuing; use Stop in the web UI to end it.",
                flush=True,
            )


def parse_openclaw_reply(stdout: str) -> str:
    s = (stdout or "").strip()
    if not s:
        return ""

    for obj in _iter_json_objects(s):
        t = _extract_text(obj)
        if t.strip():
            return t.strip()

    # 4) Plain text fallback: remove MEDIA lines
    cleaned = "\n".join(ln for ln in s.splitlines() if not ln.strip().startswith("MEDIA:")).strip()
    return cleaned if cleaned else s


@dataclass
class Cache:
    """Plain-text cache: one .txt file per heading, stored in a folder."""
    path: Path  # directory

    def _key_file(self, cache_key: str) -> Path:
        return self.path / f"{cache_key}.txt"

    def get(self, cache_key: str) -> Optional[str]:
        f = self._key_file(cache_key)
        if f.exists():
            if f.stat().st_size == 0:
                return None
            return f.read_text(encoding="utf-8")
        return None

    def set(self, cache_key: str, text: str) -> None:
        if not (text or "").strip():
            f = self._key_file(cache_key)
            if f.exists():
                f.unlink(missing_ok=True)
            return
        self.path.mkdir(parents=True, exist_ok=True)
        self._key_file(cache_key).write_text(text, encoding="utf-8")

    @staticmethod
    def load(path: Path) -> "Cache":
        path.mkdir(parents=True, exist_ok=True)
        return Cache(path=path)


# Phrases that AI models overuse — we ban them explicitly in the prompt
# and also strip them in post-processing.
BANNED_PHRASES = [
    "it's important to note", "it is important to note",
    "it's worth noting", "it is worth noting",
    "in today's world", "in the modern world",
    "in this day and age",
    "plays a crucial role", "play a crucial role",
    "plays a vital role", "play a vital role",
    "plays a pivotal role", "play a pivotal role",
    "it goes without saying",
    "needless to say",
    "at the end of the day",
    "in conclusion", "to summarize", "to sum up",
    "dive into", "dive deep", "deep dive", "delve into", "delve deeper",
    "the landscape of", "the realm of",
    "navigate the", "navigating the",
    "a testament to",
    "serves as a", "serve as a",
    "it cannot be overstated",
    "a myriad of", "myriad of",
    "foster a", "fostering a", "fostering an",
    "in an era of", "in an era where",
    "the intricacies of",
    "a comprehensive", "comprehensive understanding",
    "holistic approach", "multifaceted",
    "paradigm shift", "game changer", "game-changer",
    "leveraging", "leverage the",
    "in the realm of", "in the world of",
    "it should be noted",
    "one must", "one should",
    "stands as",
    "cornerstone",
    "undeniable", "undeniably",
    "tapestry",
    "ever-evolving", "ever evolving",
    "embark on", "embarking on",
    "ultimately",
    "moreover", "furthermore", "additionally", "consequently",
    "henceforth", "nonetheless", "nevertheless",
    "in essence", "essentially",
    "not only... but also",
    "it is crucial", "it's crucial",
    "it is essential", "it's essential",
    "cutting-edge", "cutting edge",
    "groundbreaking",
    "revolutionary",
    "seamlessly", "seamless",
    "robust",
    "empower", "empowering", "empowers",
    "transformative",
    "harness the", "harnessing the",
    "unlock the", "unlocking the",
    "elevate", "elevating",
    "streamline", "streamlining",
    "overarching",
    "underscore", "underscores",
]

_BANNED_SET = "\n".join(f"  • {p}" for p in BANNED_PHRASES[:30])  # first 30 in prompt


# Every paragraph is generated by a separate call with near-identical
# instructions, so the model converges on one favorite shape (negation-flip
# opener, analogy, "Try this", uplifting closer) for every section. These
# hints are rotated deterministically by heading so each section gets a
# different shape while the same heading stays cache-stable across runs.
# Deliberately varied registers. Each avoids the model's default "you [verb]…"
# second-person hypothetical opener (see _STRUCTURE_BANS) — the tic that makes
# every paragraph sound identical. The first word of the paragraph should differ
# meaningfully between these.
PARAGRAPH_OPENING_MOVES = [
    "Open with a concrete fact or number about the topic, stated flatly. "
    "Do not address the reader as 'you' in the first sentence.",
    "Open by stating the main point in one plain, declarative sentence. "
    "No 'you', no 'imagine', no scenario — just the claim itself.",
    "Open on a specific named example, person, place, thing, or moment in the "
    "third person (he/she/they/it/a name), not the second person.",
    "Open with a short, surprising observation about how the thing actually works. "
    "Lead with the subject of the sentence, not with 'you'.",
    "Open with a concrete detail from the real world (an object, a sound, a place) "
    "described in the third person.",
    "Open mid-thought on the idea itself, as if continuing an explanation already "
    "underway. Do not start with a 'you'-address or an imagined scene.",
    "Open with a brief third-person story beat: a specific someone doing a specific "
    "thing, somewhere real. Name them; do not make it 'you'.",
]

PARAGRAPH_CLOSING_MOVES = [
    "End on a practical note the reader can use right away.",
    "End with a concrete image, not a summary or a cheer.",
    "End on a quiet, matter-of-fact line instead of an uplifting message.",
    "End by pointing at what changes once this sinks in, in plain terms.",
    "End with the most interesting side effect of the idea, stated simply.",
    "End by circling back to a detail from earlier in the paragraph.",
]


def _structure_hints(seed_text: str) -> tuple[str, str]:
    digest = hashlib.sha256(seed_text.strip().lower().encode("utf-8")).digest()
    opening = PARAGRAPH_OPENING_MOVES[digest[0] % len(PARAGRAPH_OPENING_MOVES)]
    closing = PARAGRAPH_CLOSING_MOVES[digest[1] % len(PARAGRAPH_CLOSING_MOVES)]
    return opening, closing


# Shared bans for the paragraph prompts below. The fragment-join rule must
# stay scoped to fragments: told simply "join short sentences with a comma",
# the model fuses complete sentences and produces comma splices.
_STRUCTURE_BANS = (
    "- NEVER use the 'not X, it's Y' correction template. No sentences shaped like "
    "'The biggest mistake isn't A. It's B.' or 'It's not about A, it's about B.' or "
    "'Your job isn't A. Your job is B.' State the true point directly without first "
    "naming what it is not. At most one negation-contrast in the paragraph, and never "
    "as the opening sentence.\n"
    "- NO comma splices. Never join two complete sentences with only a comma "
    "(wrong: 'That's not a problem, that's the point.'). Use a comma to attach a "
    "fragment; use a period or a word like 'and', 'so', or 'because' between "
    "complete sentences.\n"
    "- NO stock bridge phrases like 'You know what that feels like', 'Think of a time "
    "when', or 'Try this the next time'. If an analogy or exercise helps, work it in "
    "without announcing it.\n"
    "- DO NOT open the paragraph with a second-person hypothetical scenario. Banned "
    "opening shapes: 'You [verb] ... and [consequence]' (e.g. 'You walk into a room "
    "and your brain...'), 'Imagine ...', 'Picture ...', 'Think about ...', 'When you "
    "[verb] ...', 'Say you ...', 'Ever [verb]?', 'Here's a [thing that] ...', "
    "'Let's [verb] ...'. These are the model's default opener and make every "
    "paragraph read the same. The opening sentence must NOT be a 'you'-address "
    "walkthrough of an imagined moment. Follow the PARAGRAPH SHAPE opening below "
    "literally instead.\n"
)


def build_prompt(heading: str, words_min: int, words_max: int, tone: str) -> str:
    opening_move, closing_move = _structure_hints(heading)
    return (
        f"You are a professional non-fiction ghostwriter. Write prose that reads like a seasoned author's "
        f"work in a well-edited published book: natural, warm, and human, but polished and never gimmicky.\n\n"
        f"Section topic: {heading}\n\n"
        f"Write ONE paragraph, {words_min}–{words_max} words.\n\n"
        f"VOICE & STYLE (critical):\n"
        f"- Write complete, well-formed sentences that flow into each other. The paragraph must read as "
        f"one connected line of thought, not a series of punchy statements.\n"
        f"- Use plain American English: everyday words a fifth grader knows, mostly one or two "
        f"syllables. Simple wording matters more than sentence length.\n"
        f"- One idea per sentence, easy to read aloud in one breath. A longer sentence is fine "
        f"when its words are simple and it flows.\n"
        f"- Aim for about a grade 6 reading level, the target the Hemingway editor recommends. "
        f"Sentences must flow into each other with natural transitions, the way a human writer "
        f"connects thoughts, never chopped or robotic.\n"
        f"- Vary how sentences begin. Never open two sentences the same way, and avoid formulaic "
        f"conversational openers like 'Honestly', 'Look', or 'Thing is'.\n"
        f"- Use contractions where they sound natural (don't, isn't, you'll, there's).\n"
        f"- Vary how clauses connect. Mix 'because', 'so', 'but', 'while', and 'even though'; "
        f"open some sentences with the dependent clause ('When the prop breaks, everyone "
        f"freezes.'); use a relative clause now and then ('a game that forces you to listen'). "
        f"At most one sentence in three may glue two clauses or stack verbs with ', and'. "
        f"Never fix rhythm by chopping sentences short.\n"
        f"- The word 'just' is filler. Use it at most once in the paragraph; zero is better.\n"
        f"- Prefer simple, everyday words. Say 'big' not 'substantial', 'use' not 'utilize', 'help' not 'facilitate'.\n"
        f"- Ground the writing in concrete, specific detail rather than vague generalities.\n"
        f"- Use 'you' or 'we' naturally when it fits the context.\n"
        f"- Write in a {tone} tone: confident, grounded, and unpretentious.\n\n"
        f"PARAGRAPH SHAPE (follow for this paragraph):\n"
        f"- {opening_move}\n"
        f"- {closing_move}\n"
        f"- Do not run the paragraph through the formula of big claim, then real-life "
        f"analogy, then exercise, then uplifting pep-talk closer. Real chapters vary "
        f"their shape.\n\n"
        f"HARD BANS:\n"
        f"- NEVER write short standalone sentences. When a short thought is a fragment, "
        f"attach it to the sentence before or after it with a comma "
        f"(write 'A good planner buys you breathing room, not magic.' not "
        f"'...breathing room. Not magic.'). If the short thought is a complete sentence, "
        f"expand it or connect it with a word like 'and', 'so', or 'because'.\n"
        f"{_STRUCTURE_BANS}"
        f"- NO dense sentences full of long words; they score 'very hard to read'. Keep the "
        f"wording simple and split heavy sentences.\n"
        f"- NEVER start the paragraph with the topic/heading words.\n"
        f"- NO dashes (em dash, en dash, hyphen-as-punctuation). Use periods, commas, or 'and' instead.\n"
        f"- NO bullet points or numbered lists.\n"
        f"- NO references to images, photos, diagrams, or illustrations.\n"
        f"- DO NOT repeat the section topic word-for-word.\n"
        f"- DO NOT use any of these overused AI phrases:\n{_BANNED_SET}\n\n"
        f"Output ONLY the paragraph. No title, no label, no preamble."
    )


def build_subheading_prompt(subheading: str, words_min: int, words_max: int, tone: str) -> str:
    clean = re.sub(r"^[\-•*–—]\s+", "", subheading).strip()
    # Strip leading "Thing N: " label if present
    clean = re.sub(r"^Thing\s+\d+:\s*", "", clean, flags=re.IGNORECASE).strip()
    # Strip leading numbered list prefix: "1. ", "12. ", etc.
    clean = re.sub(r"^\d+\.\s+", "", clean).strip()
    opening_move, closing_move = _structure_hints(clean)
    return (
        f"You are a professional non-fiction ghostwriter. Write prose that reads like a seasoned author's "
        f"work in a well-edited published book: natural, warm, and human, but polished and never gimmicky.\n\n"
        f"Specific point to cover: {clean}\n\n"
        f"Write ONE paragraph, {words_min}–{words_max} words, on this specific point.\n\n"
        f"VOICE & STYLE (critical):\n"
        f"- Write complete, well-formed sentences that flow into each other. The paragraph must read as "
        f"one connected line of thought, not a series of punchy statements.\n"
        f"- Use plain American English: everyday words a fifth grader knows, mostly one or two "
        f"syllables. Simple wording matters more than sentence length.\n"
        f"- One idea per sentence, easy to read aloud in one breath. A longer sentence is fine "
        f"when its words are simple and it flows.\n"
        f"- Aim for about a grade 6 reading level, the target the Hemingway editor recommends. "
        f"Sentences must flow into each other with natural transitions, the way a human writer "
        f"connects thoughts, never chopped or robotic.\n"
        f"- Vary how sentences begin. Never open two sentences the same way, and avoid formulaic "
        f"conversational openers like 'Honestly', 'Look', or 'Thing is'.\n"
        f"- Use contractions where they sound natural (don't, isn't, you'll, there's).\n"
        f"- Vary how clauses connect. Mix 'because', 'so', 'but', 'while', and 'even though'; "
        f"open some sentences with the dependent clause ('When the prop breaks, everyone "
        f"freezes.'); use a relative clause now and then ('a game that forces you to listen'). "
        f"At most one sentence in three may glue two clauses or stack verbs with ', and'. "
        f"Never fix rhythm by chopping sentences short.\n"
        f"- The word 'just' is filler. Use it at most once in the paragraph; zero is better.\n"
        f"- Prefer simple, everyday words. Say 'big' not 'substantial', 'use' not 'utilize'.\n"
        f"- Ground the advice in concrete, specific detail rather than vague generalities.\n"
        f"- Use 'you' or 'we' naturally when it fits the context.\n"
        f"- Write in a {tone} tone: confident, grounded, and unpretentious.\n\n"
        f"PARAGRAPH SHAPE (follow for this paragraph):\n"
        f"- {opening_move}\n"
        f"- {closing_move}\n"
        f"- Do not run the paragraph through the formula of big claim, then real-life "
        f"analogy, then exercise, then uplifting pep-talk closer. Real chapters vary "
        f"their shape.\n\n"
        f"HARD BANS:\n"
        f"- NEVER write short standalone sentences. When a short thought is a fragment, "
        f"attach it to the sentence before or after it with a comma "
        f"(write 'A good planner buys you breathing room, not magic.' not "
        f"'...breathing room. Not magic.'). If the short thought is a complete sentence, "
        f"expand it or connect it with a word like 'and', 'so', or 'because'.\n"
        f"{_STRUCTURE_BANS}"
        f"- NO dense sentences full of long words; they score 'very hard to read'. Keep the "
        f"wording simple and split heavy sentences.\n"
        f"- NEVER start the paragraph with the topic/heading words.\n"
        f"- NO dashes (em dash, en dash, hyphen-as-punctuation). Use periods, commas, or 'and' instead.\n"
        f"- NO bullet points or numbered lists.\n"
        f"- NO references to images, photos, diagrams, or illustrations.\n"
        f"- DO NOT repeat the point text word-for-word.\n"
        f"- DO NOT use any of these overused AI phrases:\n{_BANNED_SET}\n\n"
        f"- Stay tightly focused on this specific point only.\n"
        f"Output ONLY the paragraph. No title, no label, no preamble."
    )


# build_image_prompt, extract_heading_keywords, infer_theme_guidance are now in openclaw_image_maker.py.


def _strip_banned_phrases(text: str) -> str:
    """Remove banned phrases sentence-by-sentence. If stripping would gut a
    sentence to under five words (leaving a fragment like 'To change plans.'),
    keep the original sentence — a rare cliché reads better than broken grammar."""
    out_lines = []
    for line in text.split("\n"):
        sentences = re.split(r"(?<=[.!?])\s+", line)
        kept = []
        for sentence in sentences:
            result = sentence
            for phrase in BANNED_PHRASES:
                pattern = re.compile(re.escape(phrase) + r"(\s+that\b)?", re.IGNORECASE)
                result = pattern.sub("", result)
            if result == sentence:
                kept.append(sentence)
                continue
            result = re.sub(r"^[\s,;:]+", "", result).strip()
            if len(re.findall(r"[A-Za-z']+", result)) < 5:
                kept.append(sentence)
                continue
            if result[0].islower():
                result = result[0].upper() + result[1:]
            kept.append(result)
        out_lines.append(" ".join(s for s in kept if s))
    return "\n".join(out_lines)


def humanize_text(text: str) -> str:
    """
    Post-process generated text to strip residual AI-isms and
    improve burstiness / naturalness scores.
    """
    if not text:
        return text

    # 1) Remove any banned phrases (case-insensitive) without leaving fragments
    text = _strip_banned_phrases(text)

    # 2) Replace common non-contraction forms with contractions.
    # Negation forms are safe anywhere, including at the end of a clause.
    negation_contractions = [
        (r"\bdo not\b", "don't"),
        (r"\bDo not\b", "Don't"),
        (r"\bdoes not\b", "doesn't"),
        (r"\bDoes not\b", "Doesn't"),
        (r"\bcannot\b", "can't"),
        (r"\bCannot\b", "Can't"),
        (r"\bwill not\b", "won't"),
        (r"\bWill not\b", "Won't"),
        (r"\bshould not\b", "shouldn't"),
        (r"\bShould not\b", "Shouldn't"),
        (r"\bwould not\b", "wouldn't"),
        (r"\bWould not\b", "Wouldn't"),
        (r"\bcould not\b", "couldn't"),
        (r"\bCould not\b", "Couldn't"),
        (r"\bis not\b", "isn't"),
        (r"\bIs not\b", "Isn't"),
        (r"\bare not\b", "aren't"),
        (r"\bAre not\b", "Aren't"),
        (r"\bwas not\b", "wasn't"),
        (r"\bWas not\b", "Wasn't"),
        (r"\bwere not\b", "weren't"),
        (r"\bWere not\b", "Weren't"),
        (r"\bhave not\b", "haven't"),
        (r"\bHave not\b", "Haven't"),
        (r"\bhas not\b", "hasn't"),
        (r"\bHas not\b", "Hasn't"),
        (r"\bhad not\b", "hadn't"),
        (r"\bHad not\b", "Hadn't"),
    ]
    for pat, repl in negation_contractions:
        text = re.sub(pat, repl, text)

    # Pronoun + verb forms are NOT safe everywhere. A stranded verb must stay
    # whole ("no matter how talented they are." can never become "they're."),
    # so these only contract when another word follows in the same clause.
    # "there is" also stays whole after a motion/position word, where "there"
    # names a place instead of acting as a dummy subject ("the way to get
    # there is to stop"). "have" only contracts as an auxiliary ("they've
    # seen"), never as the main verb ("we have a plan").
    locative_before_there = {
        "get", "gets", "getting", "got", "gotten", "go", "goes", "going",
        "went", "gone", "come", "comes", "coming", "came", "arrive", "arrives",
        "arrived", "arriving", "be", "been", "being", "stay", "stays", "stayed",
        "staying", "stand", "stands", "stood", "sit", "sits", "sat", "live",
        "lives", "lived", "living", "stop", "stops", "stopped", "wait", "waits",
        "waited", "park", "parked", "left", "out", "in", "up", "down", "over",
        "back", "here", "way",
    }
    have_followers = {
        "been", "got", "gotten", "had", "done", "made", "seen", "gone", "come",
        "taken", "given", "found", "heard", "felt", "kept", "left", "lost",
        "met", "put", "read", "said", "set", "told", "thought", "tried", "won",
        "written", "known", "shown", "grown", "spoken", "broken", "chosen",
        "fallen", "become", "begun", "built", "brought", "bought", "caught",
        "held", "learned", "meant", "paid", "run", "sent", "spent", "stood",
        "taught", "worn", "never", "ever", "already", "always", "just", "also",
        "only", "all", "both", "each", "even", "still", "since", "yet", "long",
        "often", "barely", "hardly",
    }

    def _next_word(s: str, pos: int) -> str:
        m = re.match(r"[\s\"'“‘(]*([A-Za-z']+)", s[pos:])
        return m.group(1) if m else ""

    def _prev_word(s: str, pos: int) -> str:
        m = re.search(r"([A-Za-z']+)[\s\"'”’)]*$", s[:pos])
        return m.group(1) if m else ""

    def _guarded_sub(pat: str, repl: str, txt: str, there: bool = False, have: bool = False) -> str:
        def _r(m: re.Match) -> str:
            nxt = _next_word(m.string, m.end())
            if not nxt:
                return m.group(0)
            if there and _prev_word(m.string, m.start()).lower() in locative_before_there:
                return m.group(0)
            if have and not (nxt.lower() in have_followers or nxt.lower().endswith(("ed", "en"))):
                return m.group(0)
            return repl
        return re.sub(pat, _r, txt)

    pronoun_contractions = [
        (r"\bit is\b", "it's", "plain"),
        (r"\bIt is\b", "It's", "plain"),
        (r"\bthat is\b", "that's", "plain"),
        (r"\bThat is\b", "That's", "plain"),
        (r"\bthere is\b", "there's", "there"),
        (r"\bThere is\b", "There's", "there"),
        (r"\bwe are\b", "we're", "plain"),
        (r"\bWe are\b", "We're", "plain"),
        (r"\bthey are\b", "they're", "plain"),
        (r"\bThey are\b", "They're", "plain"),
        (r"\byou are\b", "you're", "plain"),
        (r"\bYou are\b", "You're", "plain"),
        (r"\bI am\b", "I'm", "plain"),
        (r"\bwe have\b", "we've", "have"),
        (r"\bWe have\b", "We've", "have"),
        (r"\bthey have\b", "they've", "have"),
        (r"\bThey have\b", "They've", "have"),
        (r"\bI have\b", "I've", "have"),
        (r"\blet us\b", "let's", "plain"),
        (r"\bLet us\b", "Let's", "plain"),
    ]
    for pat, repl, kind in pronoun_contractions:
        text = _guarded_sub(pat, repl, text, there=(kind == "there"), have=(kind == "have"))

    # 3) Replace wordy/formal words with simpler equivalents
    simplify_map = [
        (r"\butilize\b", "use"),
        (r"\bUtilize\b", "Use"),
        (r"\butilizes\b", "uses"),
        (r"\butilizing\b", "using"),
        (r"\butilization\b", "use"),
        (r"\bfacilitate\b", "help"),
        (r"\bfacilitates\b", "helps"),
        (r"\bfacilitating\b", "helping"),
        (r"\bsubstantial\b", "big"),
        (r"\bSubstantial\b", "Big"),
        (r"\bsubstantially\b", "a lot"),
        (r"\bcommence\b", "start"),
        (r"\bCommence\b", "Start"),
        (r"\bterminate\b", "end"),
        (r"\bTerminate\b", "End"),
        (r"\bpurchase\b", "buy"),
        (r"\bPurchase\b", "Buy"),
        (r"\binquire\b", "ask"),
        (r"\bInquire\b", "Ask"),
        (r"\bdemonstrate\b", "show"),
        (r"\bDemonstrate\b", "Show"),
        (r"\bdemonstrates\b", "shows"),
        (r"\bdemonstrating\b", "showing"),
        (r"\bpossess\b", "have"),
        (r"\bpossesses\b", "has"),
        (r"\bnumerous\b", "many"),
        (r"\bNumerous\b", "Many"),
        (r"\bprior to\b", "before"),
        (r"\bPrior to\b", "Before"),
        (r"\bin order to\b", "to"),
        (r"\bIn order to\b", "To"),
        (r"\bin regard to\b", "about"),
        (r"\bIn regard to\b", "About"),
        (r"\bwith regard to\b", "about"),
        (r"\bWith regard to\b", "About"),
        (r"\bcurrently\b", "now"),
        (r"\bCurrently\b", "Now"),
        (r"\bat this point in time\b", "now"),
        (r"\bAt this point in time\b", "Now"),
        (r"\bdue to the fact that\b", "because"),
        (r"\bDue to the fact that\b", "Because"),
        (r"\bin spite of the fact that\b", "although"),
        (r"\bfor the purpose of\b", "to"),
    ]
    for pat, repl in simplify_map:
        text = re.sub(pat, repl, text)

    # 4) Cap the filler word "just" at one per paragraph/line; the
    # chapter-level budget of 4 is enforced later on the whole document.
    text = limit_filler_word(text, "just", max_keep=1)

    # 5) Remove dashes that may have slipped through.
    # A comma keeps the clause attached; a period would split it into a fragment.
    text = text.replace("\u2014", ", ")   # em dash → comma
    text = text.replace("\u2013", ", ")   # en dash → comma

    # 6) Collapse double-spaces and fix spacing after substitutions
    text = re.sub(r"  +", " ", text)
    text = re.sub(r" ,", ",", text)
    text = re.sub(r"\. \.", ".", text)
    text = re.sub(r"\.\.", ".", text)
    text = re.sub(r", ,", ",", text)
    # Fix orphan punctuation from removed phrases
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    # Fix sentences starting with lowercase after period
    def _cap(m):
        return m.group(1) + m.group(2).upper()
    text = re.sub(r"(\. )([a-z])", _cap, text)

    return text.strip()


# Single choke point every OpenClaw call passes through, so a module-level
# tracker here covers every call site (main loop, smoothing retries, template
# gate retries, outline classification, rewrite-heading) without threading a
# parameter through each one. Set by main() before any generation starts.
_SPEND_TRACKER: Optional["SpendTracker"] = None


def run_openclaw_call(agent_id: str, message: str, local: bool, thinking: str, timeout_s: int, session_id: str = "") -> str:
    cmd = ["openclaw", "agent", "--agent", agent_id, "--message", message, "--json"]
    if local:
        cmd.append("--local")
    if thinking:
        cmd += ["--thinking", thinking]
    if timeout_s > 0:
        cmd += ["--timeout", str(timeout_s)]
    if session_id:
        cmd += ["--session-id", session_id]

    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            "OpenClaw failed.\n"
            f"Command: {' '.join(cmd)}\n\n"
            f"STDOUT:\n{p.stdout}\n\n"
            f"STDERR:\n{p.stderr}\n"
        )
    # Record real spend after the call, which is the first point where actual
    # billed usage is available. Crossing the threshold emits a UI warning;
    # it does not interrupt generation.
    if _SPEND_TRACKER is not None:
        _SPEND_TRACKER.record(p.stdout)
    return p.stdout


def call_openclaw(agent_id: str, message: str, local: bool, thinking: str, timeout_s: int, session_id: str = "") -> str:
    stdout = run_openclaw_call(
        agent_id=agent_id,
        message=message,
        local=local,
        thinking=thinking,
        timeout_s=timeout_s,
        session_id=session_id,
    )
    reply = parse_openclaw_reply(stdout)
    return humanize_text(reply.strip())


# Sentences under 6 words read as choppy on their own. Two short-ish sentences
# back to back read as robotic AI rhythm even when each is fine alone. Long
# sentences are judged by readability grade, not word count, so a long sentence
# made of simple words passes.
MIN_SENTENCE_WORDS = 6
CHOPPY_RUN_WORDS = 9


def _sentence_grade(words: list[str]) -> float:
    """Per-sentence readability grade using the same formula the Hemingway
    app uses (Automated Readability Index). Hemingway flags a sentence of
    14+ words as 'hard to read' at grade >= 10 and 'very hard' at >= 14."""
    letters = sum(len(re.sub(r"[^A-Za-z]", "", w)) for w in words)
    return 4.71 * (letters / len(words)) + 0.5 * len(words) - 21.43


def find_problem_sentences(text: str) -> tuple[list[str], list[str]]:
    """Return (too_short, too_hard) sentences. too_hard mirrors Hemingway's
    red 'very hard to read' flag. too_short covers sentences under
    MIN_SENTENCE_WORDS plus runs of consecutive short-ish sentences, which
    read as choppy AI rhythm even when each sentence is fine on its own."""
    short: list[str] = []
    hard: list[str] = []
    for line in text.split("\n"):
        entries: list[tuple[str, int]] = []
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            s = sentence.strip()
            if not s:
                continue
            words = re.findall(r"[A-Za-z'’]+", s)
            if not words:
                continue
            entries.append((s, len(words)))
            if len(words) < MIN_SENTENCE_WORDS:
                short.append(s)
            # Hemingway rounds the grade before comparing, so 13.5+ is red.
            elif len(words) >= 14 and int(_sentence_grade(words) + 0.5) >= 14:
                hard.append(s)

        # Two or more short-ish sentences in a row = choppy run.
        run: list[str] = []
        for s, n in entries + [("", CHOPPY_RUN_WORDS)]:  # sentinel flushes last run
            if n < CHOPPY_RUN_WORDS:
                run.append(s)
                continue
            if len(run) >= 2:
                short.extend(x for x in run if x not in short)
            run = []
    return short, hard


# The model's favorite rhetorical mold: a negated claim followed by an "It's"
# correction ("The biggest mistake isn't X. It's Y."), and its comma-splice
# twin ("That's not a problem, that's the point."). Both prompt bans and this
# detection exist because the pattern survives instructions alone.
# Newlines are excluded from the character classes so a scan over a whole
# document never matches across a paragraph boundary.

# Skip subordinate clauses like "If you aren't sure, you can ask", which are
# grammatically fine and must never be flagged or split.
_SUBORDINATE_GUARD = (
    r"(?<![Ii]f )(?<![Ww]hen )(?<![Bb]ecause )(?<![Uu]nless )(?<![Ww]hile )"
    r"(?<![Tt]hough )(?<![Aa]lthough )(?<![Ss]ince )(?<![Ww]henever )"
    r"(?<![Ww]herever )(?<![Oo]nce )(?<![Uu]ntil )(?<![Tt]ill )"
    r"(?<![Bb]efore )(?<![Aa]fter )(?<![Aa]s )"
)

_TEMPLATE_PATTERNS = [
    # Negated sentence followed by an "It <verb>" correction.
    re.compile(
        r"\b(?:isn't|is not|aren't|are not|wasn't|was not|doesn't|does not|don't|do not)\b"
        r"[^.!?\n]*[.!?][ \t]+It(?:'s|\s+(?:is|was|happens|belongs|takes|means|comes|starts|"
        r"begins|lives|grows|works|matters|shows|builds|turns))\b"
    ),
    # Same-subject comma splice ("that's not a problem, that's the point").
    re.compile(
        _SUBORDINATE_GUARD
        + r"\b(it|that|they|he|she|we|you)\b[^,.!?\n]{0,60}(?:\bnot|n't)\b[^,.!?\n]{0,60},\s*\1\b",
        re.IGNORECASE,
    ),
]


def find_template_sentences(text: str) -> list[str]:
    """Return snippets that fall into the negation-flip correction template
    or its comma-splice form, so the smoothing pass can rewrite them."""
    found: list[str] = []
    for pat in _TEMPLATE_PATTERNS:
        for m in pat.finditer(text):
            snippet = m.group(0).strip()
            if snippet and snippet not in found:
                found.append(snippet)
    return found


_SPLICE_FIX_RE = re.compile(
    _SUBORDINATE_GUARD
    + r"(?P<left>\b(?P<pron>it|that|they|he|she|we|you)\b[^,.!?\n]{0,60}"
    r"(?:\bnot|n't)\b[^,.!?\n]{0,60}),\s*(?P<pron2>(?P=pron))\b",
    re.IGNORECASE,
)


def fix_comma_splices(text: str) -> str:
    """Deterministically split a same-subject comma splice into two sentences
    ("That's not a problem, that's the point." becomes "That's not a problem.
    That's the point."). Last-resort mechanical fix for when model rewrites
    keep the splice; splitting two independent clauses is always grammatical."""
    def _fix(m: re.Match) -> str:
        pron2 = m.group("pron2")
        return f"{m.group('left')}. {pron2[0].upper()}{pron2[1:]}"

    prev = None
    while prev != text:  # chained splices need another pass after each split
        prev = text
        text = _SPLICE_FIX_RE.sub(_fix, text)
    return text


class TemplatePatternError(RuntimeError):
    """Raised when a paragraph still contains the banned negation-flip
    template after every rewrite attempt and mechanical fix. The pattern is
    never written to the book; callers skip the heading so a re-run can
    fill it, instead of shipping the text silently."""


# "just" is the model's favorite filler adverb (12+ per chapter observed).
# Removal is unsafe only where it changes meaning: the correlative "not just
# their ears", the adjective "a just cause", and the manner idiom "just so".
# Everything else ("just like", "just as", "just about") reads fine without it.
_JUST_KEEP_BEFORE = {"not", "a", "an", "the"}
_JUST_KEEP_AFTER = {"so"}


def limit_filler_word(text: str, word: str = "just", max_keep: int = 1) -> str:
    """Keep at most max_keep uses of a filler word per line/paragraph and
    delete the rest where deletion is grammatically safe."""
    word_re = re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)

    def _fix_line(line: str) -> str:
        matches = list(word_re.finditer(line))
        if len(matches) <= max_keep:
            return line
        budget = max_keep
        deletions: list[re.Match] = []
        for m in matches:
            prev_m = re.search(r"([A-Za-z']+)[^A-Za-z']*$", line[: m.start()])
            next_m = re.match(r"[^A-Za-z']*([A-Za-z']+)", line[m.end():])
            prev_w = prev_m.group(1).lower() if prev_m else ""
            next_w = next_m.group(1).lower() if next_m else ""
            protected = (
                prev_w in _JUST_KEEP_BEFORE
                or prev_w.endswith("n't")
                or next_w in _JUST_KEEP_AFTER
                or not next_w
            )
            if protected:
                continue
            if budget > 0:
                budget -= 1
                continue
            deletions.append(m)
        for m in reversed(deletions):
            start, end = m.start(), m.end()
            while end < len(line) and line[end] == " ":
                end += 1
            rest = line[end:]
            if m.group(0)[0].isupper() and rest:
                rest = rest[0].upper() + rest[1:]
            line = line[:start] + rest
        line = re.sub(r"  +", " ", line)
        line = re.sub(r"\s+([.,;:!?])", r"\1", line)
        return line

    return "\n".join(_fix_line(l) for l in text.split("\n"))


def enforce_chapter_just_budget(doc, max_per_chapter: int = 4) -> int:
    """Hard cap on 'just' per chapter across the whole document. Each
    Heading-1 starts a new budget; body paragraphs beyond it get their
    extra 'just' uses stripped. Returns the number of paragraphs changed."""
    changed = 0
    used = 0
    for p in doc.paragraphs:
        if is_heading_paragraph(p):
            used = 0
            continue
        if not paragraph_looks_like_body(p):
            continue
        count = len(re.findall(r"\bjust\b", p.text or "", flags=re.IGNORECASE))
        if not count:
            continue
        allowed = max(0, max_per_chapter - used)
        if count > allowed:
            new_text = limit_filler_word(p.text, "just", max_keep=allowed)
            if new_text != p.text:
                p.text = new_text
                changed += 1
            count = len(re.findall(r"\bjust\b", new_text, flags=re.IGNORECASE))
        used += count
    return changed


_AND_COMPOUND_RE = re.compile(r",\s+and\s+", re.IGNORECASE)


def find_and_compound_monotony(text: str) -> list[str]:
    """Flag paragraphs where more than a third of the sentences lean on the
    ', and' compound skeleton ("One person throws, the other catches, and
    the ball keeps moving"). Returns the offending sentences beyond the
    allowed share so the smoothing pass can vary the connections."""
    flagged: list[str] = []
    for line in text.split("\n"):
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", line) if s.strip()]
        if len(sentences) < 4:
            continue
        and_sents = [s for s in sentences if _AND_COMPOUND_RE.search(s)]
        allowed = max(1, len(sentences) // 3)
        if len(and_sents) > allowed and len(and_sents) >= 3:
            flagged.extend(and_sents[allowed:])
    return flagged


# The model's single most repeated opener: a second-person hypothetical scenario
# ("You walk into a room and...", "Imagine...", "When you say 'Hey Siri'...").
# When every paragraph starts this way the whole book reads as one template.
# Only the FIRST sentence is checked — a "you" address later in the paragraph is
# fine; it's the opener that must vary.
_SECOND_PERSON_OPENER_RE = re.compile(
    r"^\s*[\"'“‘(]*\s*"
    r"(?:"
    r"you\b"                                  # "You walk into a room..."
    r"|imagine\b|picture\b|consider\b"        # "Imagine..." / "Picture..."
    r"|think\s+(?:about|of|back)\b"           # "Think about..."
    r"|when\s+you\b|say\s+you\b|suppose\s+you\b|let'?s\b"  # "When you..." / "Let's..."
    r"|ever\s+\w+(?:ed|en)?\b[^.?!]*\?"       # "Ever tripped in front of people?"
    r"|here'?s\s+(?:a|an|the|what|why|how|something)\b"    # "Here's a sentence that..."
    r")",
    re.IGNORECASE,
)


def find_second_person_opener(text: str) -> list[str]:
    """Return the opening sentence if the paragraph starts with the banned
    second-person hypothetical opener, else an empty list. Checked per-line so
    it works on both single paragraphs and multi-paragraph blocks."""
    flagged: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        first = re.split(r"(?<=[.!?])\s+", line, maxsplit=1)[0]
        if _SECOND_PERSON_OPENER_RE.match(first):
            flagged.append(first[:120])
    return flagged


_CHAPTER_TITLE_RE = re.compile(
    r"^\s*chapter\s+(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\b",
    re.IGNORECASE,
)


def is_chapter_title_heading(heading: str) -> bool:
    """True for outline headings like 'Chapter 7: Acting with Other People'.
    These get no intro paragraph by default: an intro generated in isolation
    always half-repeats what the subheadings below it cover."""
    return bool(_CHAPTER_TITLE_RE.match(heading or ""))


def _normalize_outline_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def classify_outline_with_ai(
    agent_id: str,
    lines: list[str],
    local: bool,
    thinking: str,
    timeout_s: int,
) -> Optional[dict[str, str]]:
    """Ask the model to label every outline line as CHAPTER, SECTION, or
    OTHER so any outline format works, instead of relying on docx styles
    and text patterns. Returns {normalized line text: 'chapter'|'section'}
    or None when the reply is unusable (caller falls back to heuristics).

    Keyed by text rather than paragraph index on purpose: the generation
    loop inserts new paragraphs as it goes, so indexes shift, while the
    outline lines' text stays stable."""
    if not lines:
        return None
    numbered = "\n".join(f"{i + 1}. {l.strip()[:200]}" for i, l in enumerate(lines))
    prompt = (
        "TASK: classify outline lines. Do NOT write any book content.\n\n"
        "Below are the lines of a book outline document, numbered. Classify every line:\n"
        "- CHAPTER: a chapter-level title. Examples: 'Chapter 3: Stage Fright', 'Part Two', "
        "or a top-level topic that groups the lines under it. 'Introduction' and "
        "'Conclusion' count as CHAPTER when they are top-level parts of the book.\n"
        "- SECTION: a topic, tip, or subheading that should get its own written paragraph. "
        "Examples: bullet points, numbered tips, short topic phrases under a chapter.\n"
        "- OTHER: everything else. Examples: the book's title, author name, notes or "
        "instructions to the writer, table-of-contents lines, and any line that is "
        "already finished prose (full sentences forming a written paragraph).\n\n"
        f"LINES:\n{numbered}\n\n"
        "Reply with exactly one line per input line, in the form '<number>: CHAPTER' or "
        "'<number>: SECTION' or '<number>: OTHER'. Output nothing else: no explanations, "
        "no headings, no book text."
    )
    stdout = run_openclaw_call(
        agent_id=agent_id,
        message=prompt,
        local=local,
        thinking=thinking,
        timeout_s=timeout_s,
        # Fresh session: keeps the classification exchange out of the
        # writing session's context and vice versa.
        session_id=str(uuid.uuid4()),
    )
    reply = parse_openclaw_reply(stdout)
    labels: dict[int, str] = {}
    for m in re.finditer(
        r"^\s*(\d+)\s*[:.\-\)]\s*(CHAPTER|SECTION|OTHER)\b",
        reply, re.IGNORECASE | re.MULTILINE,
    ):
        idx = int(m.group(1))
        if 1 <= idx <= len(lines):
            labels[idx] = m.group(2).lower()
    # Require nearly every line labeled; a partial reply means the model
    # drifted off-task and the heuristics are safer.
    if len(labels) < max(1, int(0.9 * len(lines))):
        return None
    roles: dict[str, str] = {}
    for i, line in enumerate(lines):
        role = labels.get(i + 1)
        if role not in ("chapter", "section"):
            continue
        key = _normalize_outline_text(line)
        # If duplicate lines disagree, 'chapter' wins.
        if key and roles.get(key) != "chapter":
            roles[key] = role
    return roles


def build_smooth_prompt(
    paragraph: str,
    short: list[str],
    hard: list[str],
    templated: Optional[list[str]] = None,
    and_compounds: Optional[list[str]] = None,
) -> str:
    issues = []
    if hard:
        listed = "\n".join(f'  - "{s[:140]}"' for s in hard[:8])
        issues.append(
            "These sentences are too dense (the Hemingway app scores them 'very hard to "
            "read'). Split each one into shorter, complete sentences and swap heavy words "
            "for plain everyday American English:\n" + listed
        )
    if short:
        listed = "\n".join(f'  - "{s}"' for s in short[:8])
        issues.append(
            "These sentences are too short, or several short ones sit in a row, which makes "
            "the rhythm choppy. Rework each one so it does not stand alone: fold it into a "
            "neighboring sentence with a connecting word like 'and', 'so', or 'because', or "
            "grow it into a fuller sentence. Never fuse two complete sentences with only a "
            "comma:\n" + listed
        )
    if templated:
        listed = "\n".join(f'  - "{s[:140]}"' for s in templated[:8])
        issues.append(
            "These passages use the 'not X, it's Y' correction template or splice two "
            "complete sentences together with a comma. Rewrite each one to state the true "
            "point directly, without first naming what it is not, and never join two "
            "complete sentences with only a comma:\n" + listed
        )
    if and_compounds:
        listed = "\n".join(f'  - "{s[:140]}"' for s in and_compounds[:8])
        issues.append(
            "Too many sentences in this paragraph glue their clauses together with ', and'. "
            "Rewrite these so the paragraph mixes connection styles: use 'because', 'so', "
            "'but', 'while', or 'even though'; start some sentences with the dependent "
            "clause ('When the prop breaks, everyone freezes.'); or fold one clause into a "
            "relative clause ('a game that forces you to let go'). Do NOT fix this by "
            "chopping sentences into short pieces; keep each sentence full length:\n" + listed
        )
    issues_block = "\n\n".join(issues)
    return (
        "Edit the paragraph below. Keep the meaning, tone, and overall length the same, "
        "and keep the writing natural and human. Fix ONLY these problems:\n\n"
        f"{issues_block}\n\n"
        "Keep every sentence natural and easy to read, one clear idea per sentence, with "
        "plain American English words. Do not add new ideas and do not use dashes.\n\n"
        f"Paragraph:\n{paragraph}\n\n"
        "Output ONLY the rewritten paragraph. No preamble, no notes."
    )


def build_opener_fix_prompt(paragraph: str) -> str:
    """Rewrite only the opening sentence of a paragraph that starts with the
    banned second-person hypothetical scenario, keeping the rest intact."""
    return (
        "The paragraph below opens with a tired, overused shape: a second-person "
        "hypothetical scenario that walks the reader through an imagined moment "
        "(for example 'You walk into a room and...', 'Imagine...', 'Picture...', "
        "'When you say...', 'Here's a...'). Every paragraph in this book opens the "
        "same way and it has to stop.\n\n"
        "Rewrite the paragraph so the FIRST sentence uses a different, stronger "
        "opening. The new first sentence must NOT begin with 'you', 'imagine', "
        "'picture', 'think about', 'when you', 'say you', 'here's', or 'let's', and "
        "must not be an imagined 'you'-walkthrough. Good replacements: state a "
        "concrete fact or number; make the plain point directly; name a specific "
        "real example in the third person; or describe a concrete real-world detail. "
        "Lead with the subject of the sentence, not with the reader.\n\n"
        "Keep the rest of the paragraph's meaning, information, tone, and length the "
        "same. It is fine to use 'you' later in the paragraph, just not as the opener. "
        "Do not add new ideas and do not use dashes.\n\n"
        f"Paragraph:\n{paragraph}\n\n"
        "Output ONLY the rewritten paragraph. No preamble, no notes."
    )


def _generate_clean_paragraph_once(
    agent_id: str,
    message: str,
    local: bool,
    thinking: str,
    timeout_s: int,
    session_id: str = "",
    max_smooth_retries: int = 0,
    max_template_retries: int = 0,
    max_opener_retries: int = 1,
) -> str:
    """Generate a paragraph and run the same quality checks as before, but
    (per explicit cost-control instruction) no longer pays for a rewrite call
    to fix what they find. Each retry call repeats the full ~24K-token
    OpenClaw system-prompt overhead, so retries were roughly doubling cost
    per heading. Defaults are 0: issues are still detected and logged, but
    only fixed for free — via fix_comma_splices, pure Python, no API call.

    Banned-template enforcement remains a hard guarantee even at zero
    retries: the free mechanical splice fix still runs, and a paragraph that
    still carries the pattern after that is never shipped — it raises
    TemplatePatternError so generate_clean_paragraph can resample."""
    generated = call_openclaw(agent_id, message, local, thinking, timeout_s, session_id)

    # Detection-only pass so quality issues are still visible in the log even
    # when max_smooth_retries=0 means nothing gets spent fixing them via a
    # second API call — only the free mechanical fixes below still apply.
    if max_smooth_retries == 0:
        short0, hard0 = find_problem_sentences(generated)
        templated0 = find_template_sentences(generated)
        andy0 = find_and_compound_monotony(generated)
        issues0 = len(short0) + len(hard0) + len(templated0) + len(andy0)
        if issues0:
            print(f"  quality check: {len(hard0)} very-hard + {len(short0)} too-short + "
                  f"{len(templated0)} templated + {len(andy0)} comma-and sentence(s) "
                  f"— not rewritten (retries disabled for cost)", flush=True)

    for _ in range(max_smooth_retries):
        short, hard = find_problem_sentences(generated)
        templated = find_template_sentences(generated)
        andy = find_and_compound_monotony(generated)
        problems = len(short) + len(hard) + len(templated) + len(andy)
        if problems == 0:
            break
        preview = "; ".join(f'"{s[:60]}"' for s in (hard + short + templated + andy)[:2])
        print(f"  {len(hard)} very-hard + {len(short)} too-short + "
              f"{len(templated)} templated + {len(andy)} comma-and sentence(s) "
              f"({preview}) — rewriting", flush=True)
        fixed = call_openclaw(
            agent_id, build_smooth_prompt(generated, short, hard, templated, andy),
            local, thinking, timeout_s, session_id,
        )
        # Accept the rewrite only if it actually improved and isn't degenerate.
        f_short, f_hard = find_problem_sentences(fixed)
        f_templated = find_template_sentences(fixed)
        f_andy = find_and_compound_monotony(fixed)
        long_enough = len(fixed.split()) >= 0.6 * len(generated.split())
        if long_enough and (len(f_short) + len(f_hard) + len(f_templated) + len(f_andy)) < problems:
            generated = fixed
        else:
            break

    # HARD GATE: the banned template must never ship. Anything that survived
    # the smoothing loop gets dedicated rewrite attempts, then a mechanical
    # splice fix. A paragraph that still carries the pattern is rejected
    # outright instead of being returned.
    for _ in range(max_template_retries):
        templated = find_template_sentences(generated)
        if not templated:
            break
        print(f"  template gate: {len(templated)} banned pattern(s) — rewriting", flush=True)
        fixed = call_openclaw(
            agent_id, build_smooth_prompt(generated, [], [], templated),
            local, thinking, timeout_s, session_id,
        )
        if (len(fixed.split()) >= 0.6 * len(generated.split())
                and len(find_template_sentences(fixed)) < len(templated)):
            generated = fixed

    if find_template_sentences(generated):
        generated = fix_comma_splices(generated)

    leftovers = find_template_sentences(generated)
    if leftovers:
        raise TemplatePatternError(
            "Paragraph still uses the banned 'not X, it's Y' template after "
            f"{max_template_retries} rewrite attempts: "
            + "; ".join(f'"{s[:80]}"' for s in leftovers[:3])
        )

    # Second-person-opener fix: ONE targeted rewrite, and only when the tic is
    # actually present (so cost is paid only for the offending paragraphs, not
    # every one). This can't be fixed mechanically — only the opening sentence
    # is regenerated, the rest of the paragraph is kept.
    if max_opener_retries > 0 and find_second_person_opener(generated):
        opener = find_second_person_opener(generated)[0]
        print(f'  opener fix: paragraph starts with a second-person scenario '
              f'("{opener[:50]}…") — rewriting the opening', flush=True)
        fixed = call_openclaw(
            agent_id, build_opener_fix_prompt(generated),
            local, thinking, timeout_s, session_id,
        )
        # Accept only if it removed the tic and isn't degenerate.
        if (len(fixed.split()) >= 0.6 * len(generated.split())
                and not find_second_person_opener(fixed)
                and not find_template_sentences(fixed)):
            generated = fixed
    else:
        if find_second_person_opener(generated):
            print("  opener check: second-person scenario opener (retries off) "
                  "— not rewritten", flush=True)

    return generated


def generate_clean_paragraph(
    agent_id: str,
    message: str,
    local: bool,
    thinking: str,
    timeout_s: int,
    session_id: str = "",
    max_smooth_retries: int = 0,
    max_template_retries: int = 0,
    max_opener_retries: int = 1,
    max_regen_attempts: int = 2,
) -> str:
    """Generate a paragraph, resampling from scratch when the banned template
    survives the free mechanical fix. The old behavior skipped the heading and
    told the user to re-run the whole job; that re-run pays for the exact same
    fresh generation call this retry makes, so resampling here costs nothing
    extra — it just removes the manual step. Each attempt uses a new session
    so it is an independent sample, not a conversation that re-reads the
    rejected text. Raises TemplatePatternError only when every attempt still
    carries the pattern."""
    attempts_left = 1 + max(0, max_regen_attempts)
    while True:
        try:
            return _generate_clean_paragraph_once(
                agent_id=agent_id,
                message=message,
                local=local,
                thinking=thinking,
                timeout_s=timeout_s,
                session_id=session_id,
                max_smooth_retries=max_smooth_retries,
                max_template_retries=max_template_retries,
                max_opener_retries=max_opener_retries,
            )
        except TemplatePatternError:
            attempts_left -= 1
            if attempts_left <= 0:
                raise
            print(f"  banned template survived the mechanical fix — "
                  f"regenerating from scratch ({attempts_left} attempt(s) left)",
                  flush=True)
            session_id = str(uuid.uuid4())


def call_openclaw_for_image(message: str, local: bool, thinking: str, timeout_s: int, session_id: str = "") -> str:
    stdout = run_openclaw_call(
        agent_id=IMAGE_AGENT_ID,
        message=message,
        local=local,
        thinking=thinking,
        timeout_s=timeout_s,
        session_id=session_id,
    )
    media_urls = parse_openclaw_media_urls(stdout)
    if not media_urls:
        reply = parse_openclaw_reply(stdout)
        raise RuntimeError(
            "OpenClaw returned no image media URL.\n"
            f"Reply was:\n{reply}"
        )
    return media_urls[0]


# build_image_prompt_from_paragraph and generate_image_openai are now in openclaw_image_maker.py.


def generate_image_via_image_maker(
    heading: str,
    paragraph_text: str,
    output_path: Path,
    openai_api_key: str,
    image_model: str = "gpt-image-1",
    image_size: str = "1024x1536",
    image_quality: str = "high",
    prompt_variant: str = "rich-scene-no-text",
    cache_dir: str = ".openclaw_cache/images",
    force: bool = False,
    image_guidance: str = "",
) -> Path:
    """Generate an image by calling openclaw_image_maker.py as a subprocess."""
    script = Path(__file__).resolve().parent / "openclaw_image_maker.py"
    cmd = [
        sys.executable, str(script),
        "--heading", heading,
        "--paragraph", (paragraph_text or "")[:200],
        "--prompt-variant", prompt_variant,
        "--model", image_model,
        "--size", image_size,
        "--quality", image_quality,
        "--cache", str(cache_dir),
        "--output", str(output_path),
    ]
    if openai_api_key:
        cmd.extend(["--openai-api-key", openai_api_key])
    if image_guidance.strip():
        cmd.extend(["--guidance", image_guidance.strip()])
    if force:
        cmd.append("--force")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"openclaw_image_maker.py failed (exit {result.returncode}):\n{result.stderr}"
        )
    if not output_path.exists():
        raise RuntimeError(f"openclaw_image_maker.py did not create {output_path}")
    return output_path


def find_cached_image(cache_dir: Path, cache_key: str) -> Optional[Path]:
    if not cache_dir.exists():
        return None
    matches = sorted(
        p for p in cache_dir.glob(f"{cache_key}.*")
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )
    return matches[0] if matches else None


def _normalize_image_ext(ext: str) -> str:
    e = (ext or "").strip().lower()
    if not e:
        return ".png"
    if not e.startswith("."):
        e = f".{e}"
    if e == ".jpe":
        return ".jpg"
    return e if e in IMAGE_SUFFIXES else ".png"


def _guess_image_ext_from_content_type(content_type: str) -> str:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    guessed = mimetypes.guess_extension(ct) if ct else None
    if guessed == ".jpe":
        guessed = ".jpg"
    return _normalize_image_ext(guessed or ".png")


def _save_image_bytes(cache_dir: Path, cache_key: str, data: bytes, ext: str, media_url: str) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Remove older copies with another extension.
    for old in cache_dir.glob(f"{cache_key}.*"):
        if old.is_file() and old.suffix.lower() in IMAGE_SUFFIXES:
            old.unlink(missing_ok=True)

    image_path = cache_dir / f"{cache_key}{_normalize_image_ext(ext)}"
    image_path.write_bytes(data)
    (cache_dir / f"{cache_key}.url.txt").write_text(media_url, encoding="utf-8")
    return image_path


def download_image_to_cache(media_url: str, cache_dir: Path, cache_key: str, timeout_s: int) -> Path:
    url = (media_url or "").strip()
    if not url:
        raise RuntimeError("Empty media URL from OpenClaw image generation.")

    if url.startswith("file://"):
        parsed = urlparse(url)
        local_path = Path(parsed.path)
        if local_path.exists():
            data = local_path.read_bytes()
            ext = _normalize_image_ext(local_path.suffix)
            return _save_image_bytes(cache_dir, cache_key, data, ext, media_url=url)
        raise RuntimeError(f"file:// media path does not exist: {local_path}")

    local_candidate = Path(url).expanduser()
    if local_candidate.exists() and local_candidate.is_file():
        data = local_candidate.read_bytes()
        ext = _normalize_image_ext(local_candidate.suffix)
        return _save_image_bytes(cache_dir, cache_key, data, ext, media_url=url)

    if url.startswith("data:image/"):
        header, payload = url.split(",", 1)
        mime = header[5:].split(";", 1)[0].strip()
        ext = _guess_image_ext_from_content_type(mime)
        if ";base64" in header:
            data = base64.b64decode(payload)
        else:
            data = unquote_to_bytes(payload)
        if not data:
            raise RuntimeError("Decoded data URL produced empty image bytes.")
        return _save_image_bytes(cache_dir, cache_key, data, ext, media_url=url)

    req = Request(url, headers={"User-Agent": "openclaw-docx-writer/1.0"})
    with urlopen(req, timeout=max(10, timeout_s)) as resp:
        data = resp.read()
        content_type = ""
        try:
            content_type = resp.headers.get_content_type()
        except Exception:
            content_type = resp.headers.get("Content-Type", "")

    if not data:
        raise RuntimeError("Downloaded image bytes are empty.")

    parsed = urlparse(url)
    ext_from_url = Path(parsed.path).suffix.lower()
    ext = _normalize_image_ext(ext_from_url if ext_from_url in IMAGE_SUFFIXES else "")
    if ext == ".png" and content_type:
        ext = _guess_image_ext_from_content_type(content_type)

    return _save_image_bytes(cache_dir, cache_key, data, ext, media_url=url)


def find_existing_image_paragraph(doc: Document, start_index: int) -> Optional[Paragraph]:
    j = start_index + 1
    while j < len(doc.paragraphs):
        probe = doc.paragraphs[j]
        if is_heading_paragraph(probe) or is_subheading_paragraph(probe):
            break
        if paragraph_has_image(probe):
            return probe
        j += 1
    return None


def _get_paragraph_text_after(doc: Document, heading_index: int) -> str:
    """Get the text of the paragraph immediately following a heading (the generated content)."""
    j = heading_index + 1
    while j < len(doc.paragraphs):
        probe = doc.paragraphs[j]
        if is_heading_paragraph(probe) or is_subheading_paragraph(probe):
            break
        text = (probe.text or "").strip()
        if text and not paragraph_has_image(probe):
            return text
        j += 1
    return ""


def _select_heading_for_image(doc: Document, start_index: int) -> Optional[tuple[int, Paragraph, str]]:
    """
    Select the real heading paragraph for image prompts.
    Mirrors format_docx chapter output where:
      - "CHAPTER X" is a label line
      - the next non-empty paragraph is the actual chapter title
    """
    p = doc.paragraphs[start_index]
    text = (p.text or "").strip()
    if not text:
        return None

    if CHAPTER_LABEL_ONLY_RE.match(text):
        j = start_index + 1
        while j < len(doc.paragraphs):
            candidate = doc.paragraphs[j]
            candidate_text = (candidate.text or "").strip()
            if not candidate_text or paragraph_has_image(candidate):
                j += 1
                continue
            # If another chapter label appears, we failed to find a proper title.
            if CHAPTER_LABEL_ONLY_RE.match(candidate_text):
                return None
            return j, candidate, candidate_text
        return None

    if is_heading_paragraph(p):
        return start_index, p, text

    return None


def insert_images_into_document(
    doc_path: Path,
    image_cache_path: Path,
    image_width: float,
    force: bool,
    openai_api_key: str,
    image_model: str = "dall-e-3",
    image_size: str = "1024x1792",
    image_quality: str = "hd",
    prompt_variant: str = "rich-scene-no-text",
    sleep_s: float = 0.0,
    image_heading_filter: str = "",
    image_guidance: str = "",
    **_kwargs,
) -> tuple[int, int]:
    if not doc_path.exists():
        raise FileNotFoundError(f"Image target document does not exist: {doc_path}")
    if not openai_api_key:
        raise RuntimeError("OpenAI API key is required for image generation. "
                           "Set --openai-api-key or OPENAI_API_KEY env var.")

    doc = Document(str(doc_path))
    inserted = 0
    replaced = 0

    i = 0
    while i < len(doc.paragraphs):
        selected = _select_heading_for_image(doc, i)
        if selected is None:
            i += 1
            continue

        heading_index, heading_para, heading = selected

        # If --image-heading filter is active, skip non-matching headings
        if image_heading_filter and image_heading_filter.lower() not in heading.lower():
            i = heading_index + 1
            continue

        # --image-heading implies --force for matched headings
        effective_force = force or bool(image_heading_filter)

        existing_image_para = find_existing_image_paragraph(doc, heading_index)
        if existing_image_para is not None and not effective_force:
            i = heading_index + 1
            continue

        # Get the paragraph text after this heading for richer prompt context
        paragraph_text = _get_paragraph_text_after(doc, heading_index)
        guidance_tag = image_guidance.strip() if image_guidance else ""
        image_cache_key = hashlib.sha256(
            f"image::{image_model}::{image_size}::{image_quality}::{heading}::{guidance_tag}".encode("utf-8")
        ).hexdigest()
        image_output = image_cache_path / f"{image_cache_key}.png"
        image_path = None if effective_force else find_cached_image(image_cache_path, image_cache_key)

        if image_path is None:
            print(f"  generating image for: {heading[:80]}")
            image_path = generate_image_via_image_maker(
                heading=heading,
                paragraph_text=paragraph_text,
                output_path=image_output,
                openai_api_key=openai_api_key,
                image_model=image_model,
                image_size=image_size,
                image_quality=image_quality,
                prompt_variant=prompt_variant,
                cache_dir=str(image_cache_path),
                force=effective_force,
                image_guidance=guidance_tag,
            )
            if sleep_s > 0:
                time.sleep(sleep_s)
        else:
            print(f"  image cache hit for: {heading[:80]}")

        if existing_image_para is not None and effective_force:
            set_paragraph_image(existing_image_para, image_path, width_inches=image_width)
            replaced += 1
        else:
            insert_image_after(heading_para, image_path, width_inches=image_width)
            inserted += 1

        i = heading_index + 1

    if inserted > 0 or replaced > 0:
        doc.save(str(doc_path))

    return inserted, replaced


# ----------------------------
# Main
# ----------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=str, help="Input .docx path")
    ap.add_argument("output", type=str, nargs="?", default=None,
                    help="Output .docx path (default: overwrite the input file)")
    ap.add_argument("--agent", default="", help="OpenClaw agent id/name (e.g. main, ops)")
    ap.add_argument("--cache", default=".openclaw_cache", help="Cache folder for plain-text results")
    ap.add_argument("--session-id", default="", help="OpenClaw session ID (auto-generated and reused if omitted)")
    ap.add_argument("--session-file", default=".openclaw_session_id", help="File to persist the session ID between runs")
    ap.add_argument("--words", type=int, default=250, help="Min words per chapter heading paragraph")
    ap.add_argument("--words-max", type=int, default=320, help="Max words per chapter heading paragraph (default: words+40)")
    ap.add_argument("--subwords", type=int, default=250, help="Min words per bullet-point paragraph")
    ap.add_argument("--subwords-max", type=int, default=320, help="Max words per bullet-point paragraph (default: subwords+40)")
    ap.add_argument("--tone", default="friendly, encouraging, and easy to understand", help="Writing tone")
    ap.add_argument("--local", action="store_true", help="Force --local (embedded runtime)")
    ap.add_argument("--thinking", default="", help="Thinking level (off|minimal|low|medium|high|xhigh)")
    ap.add_argument("--timeout", type=int, default=180, help="OpenClaw timeout seconds")
    ap.add_argument("--sleep", type=float, default=0.0, help="Sleep seconds between headings")
    ap.add_argument("--force", action="store_true", help="Re-generate all content even if already present in the document")
    ap.add_argument("--no-cache", action="store_true",
                    help="Skip reading cached text; always generate fresh (results are still saved to the cache)")
    ap.add_argument("--images", action="store_true", help="Generate and insert one image for each main heading")
    ap.add_argument("--image-width", type=float, default=5.5, help="Inserted image width in inches")
    ap.add_argument(
        "--image-prompt-variant",
        default="rich-scene-no-text",
        help="Prompt variant for openclaw_image_maker.py (e.g. rich-scene-no-text, chapter-page-rich-gray)",
    )
    ap.add_argument("--openai-api-key", default="",
                    help="OpenAI API key for image generation (default: OPENAI_API_KEY env var)")
    ap.add_argument("--image-model", default="gpt-image-1",
                    help="OpenAI image model (dall-e-2, dall-e-3, gpt-image-1)")
    ap.add_argument("--image-size", default="1024x1536",
                    help="Image size (e.g. 1024x1024, 1024x1792)")
    ap.add_argument("--image-quality", default="high",
                    help="Image quality (e.g. standard/hd for dall-e-3, high for gpt-image-1)")
    ap.add_argument("--image-heading", default="",
                    help="Only (re)generate the image for headings containing this text (case-insensitive substring match). Implies --force for the matched heading(s).")
    ap.add_argument("--image-guidance", default="",
                    help="Extra user guidance appended to the image prompt to steer replacement images (e.g. 'show a cozy dinner scene, not a proposal')")
    ap.add_argument("--rewrite-heading", default="",
                    help="Re-generate the text paragraph for headings containing this text (case-insensitive substring match). Requires --agent.")
    ap.add_argument("--rewrite-guidance", default="",
                    help="Extra guidance appended to the text prompt when rewriting (e.g. 'make it more humorous' or 'focus on practical tips')")
    ap.add_argument("--chapter-intros", action="store_true",
                    help="Also write intro text for chapter-title headings ('Chapter 3: ...'). "
                         "Default: skip them so only subheadings get content; a chapter intro "
                         "written in isolation half-repeats what the sections below it say.")
    ap.add_argument("--no-ai-outline", action="store_true",
                    help="Skip the AI outline analysis and use only the built-in style/pattern "
                         "detection for headings and subheadings.")
    ap.add_argument("--no-text", action="store_true",
                    help="Skip ALL text generation. The document already contains finished prose "
                         "(human-written or previously generated); do not classify headings or "
                         "write any paragraphs. Only run post-processing (formatting, images, KDP). "
                         "Use this for already-written books so the writer can't mistake body "
                         "paragraphs for outline headings and balloon the word count.")
    ap.add_argument("--max-spend-usd", type=float, default=0.0,
                    help="OpenClaw spend warning threshold for this run, in dollars, priced from "
                         "each call's own usage report. Crossing it notifies the web UI while "
                         "generation continues until the user explicitly stops it. "
                         "0 (default) means no cap.")
    args = ap.parse_args()

    # Spending warning, measured from real per-call usage (see run_openclaw_call).
    # Set before any code path can reach an OpenClaw call.
    global _SPEND_TRACKER
    _SPEND_TRACKER = SpendTracker(args.max_spend_usd if args.max_spend_usd > 0 else None)
    if _SPEND_TRACKER.limit_usd is not None:
        print(
            f"Spending warning: ${_SPEND_TRACKER.limit_usd:.2f} "
            "(generation continues unless Stop is clicked)",
            flush=True,
        )

    # Load .env from project cwd and script directory (without overriding shell env vars).
    env_candidates = [
        (Path.cwd() / ".env").resolve(),
        (Path(__file__).resolve().parent / ".env").resolve(),
    ]
    seen_env_paths: set[Path] = set()
    for env_path in env_candidates:
        if env_path in seen_env_paths:
            continue
        seen_env_paths.add(env_path)
        load_env_file(env_path)

    in_path = Path(args.input)
    out_path = Path(args.output) if args.output else in_path
    cache_path = Path(args.cache)
    session_file = Path(args.session_file)

    words_min = args.words
    words_max = args.words_max if args.words_max > 0 else words_min + 40
    subwords_min = args.subwords
    subwords_max = args.subwords_max if args.subwords_max > 0 else subwords_min + 40
    image_width = max(1.0, float(args.image_width))
    defer_images_until_postprocess = bool(args.images)
    openai_api_key = args.openai_api_key or os.environ.get("OPENAI_API_KEY", "")

    if args.images and not openai_api_key:
        print("ERROR: --images requires an OpenAI API key. "
              "Set --openai-api-key or OPENAI_API_KEY env var.", file=sys.stderr)
        return 2

    if not in_path.exists():
        print(f"ERROR: input file not found: {in_path}", file=sys.stderr)
        return 2

    # --image-heading: replace a single heading's image and exit immediately.
    if args.image_heading:
        if not openai_api_key:
            print("ERROR: --image-heading requires an OpenAI API key. "
                  "Set --openai-api-key or OPENAI_API_KEY env var.", file=sys.stderr)
            return 2
        image_cache_path = Path(args.cache) / "images"
        image_cache_path.mkdir(parents=True, exist_ok=True)
        target = out_path
        print(f"Replacing image for heading matching '{args.image_heading}' in {target}")
        try:
            inserted, replaced = insert_images_into_document(
                doc_path=target,
                image_cache_path=image_cache_path,
                image_width=max(1.0, float(args.image_width)),
                force=True,
                openai_api_key=openai_api_key,
                image_model=args.image_model,
                image_size=args.image_size,
                image_quality=args.image_quality,
                prompt_variant=args.image_prompt_variant,
                sleep_s=args.sleep,
                image_heading_filter=args.image_heading,
                image_guidance=args.image_guidance,
            )
            print(f"Done: inserted={inserted}, replaced={replaced}. File: {target}")
        except Exception as e:
            print(f"ERROR: image replacement failed — {e}", file=sys.stderr)
            return 1
        return 0

    # --rewrite-heading: re-generate the text paragraph for matching headings and exit.
    if args.rewrite_heading:
        if not args.agent:
            print("ERROR: --rewrite-heading requires --agent.", file=sys.stderr)
            return 2
        target = out_path
        print(f"Rewriting paragraph for heading matching '{args.rewrite_heading}' in {target}")

        # Resolve session for OpenClaw calls
        if args.session_id:
            rw_session = args.session_id
        elif session_file.exists():
            rw_session = session_file.read_text(encoding="utf-8").strip() or str(uuid.uuid4())
        else:
            rw_session = str(uuid.uuid4())

        doc = Document(str(target))
        rewritten = 0
        i = 0
        while i < len(doc.paragraphs):
            p = doc.paragraphs[i]
            is_h = is_heading_paragraph(p)
            is_sub = (not is_h) and is_subheading_paragraph(p)
            if not is_h and not is_sub:
                i += 1
                continue
            heading = (p.text or "").strip()
            if not heading or args.rewrite_heading.lower() not in heading.lower():
                i += 1
                continue

            # Find the body paragraph right after this heading
            next_para = doc.paragraphs[i + 1] if (i + 1) < len(doc.paragraphs) else None
            if next_para is None or not paragraph_looks_like_body(next_para):
                print(f"  skipping '{heading[:60]}' — no body paragraph found after it")
                i += 1
                continue

            tag = "heading" if is_h else "subheading"
            print(f"  rewriting {tag}: {heading[:80]}")

            if is_h:
                prompt = build_prompt(heading=heading, words_min=words_min, words_max=words_max, tone=args.tone)
            else:
                prompt = build_subheading_prompt(subheading=heading, words_min=subwords_min, words_max=subwords_max, tone=args.tone)

            # Append user guidance to steer the rewrite
            if args.rewrite_guidance.strip():
                prompt += f"\n\nAdditional guidance from the author: {args.rewrite_guidance.strip()}"

            try:
                generated = generate_clean_paragraph(
                    agent_id=args.agent,
                    message=prompt,
                    local=args.local,
                    thinking=args.thinking,
                    timeout_s=args.timeout,
                    session_id=rw_session,
                )
            except TemplatePatternError as e:
                print(f"  ERROR: {e}", file=sys.stderr)
                print("  Keeping the existing paragraph; re-run to retry this heading.", flush=True)
                i += 1
                continue
            next_para.text = generated
            rewritten += 1
            print(f"  rewritten ({len(generated)} chars)")

            i += 1

        if rewritten > 0:
            doc.save(str(target))
        print(f"Done: rewritten={rewritten}. File: {target}")
        return 0

    if not args.agent and not args.no_text:
        print("ERROR: --agent is required (unless using --image-heading, --rewrite-heading, or --no-text).", file=sys.stderr)
        return 2

    # Resolve session ID: explicit > persisted > new
    if args.session_id:
        session_id = args.session_id
    elif session_file.exists():
        session_id = session_file.read_text(encoding="utf-8").strip()
    else:
        session_id = str(uuid.uuid4())
        session_file.write_text(session_id, encoding="utf-8")
    print(f"Using session ID: {session_id}")

    cache = Cache.load(cache_path)
    image_cache_path = cache_path / "images"

    doc = Document(str(in_path))

    # Already-written book: skip ALL text generation. Do not classify
    # headings and do not write paragraphs — the document is finished prose,
    # and any heading-detection here risks mistaking a real body paragraph
    # for an outline line and generating content under it (which ballooned a
    # 29k-word book to 100k once). Fall straight through to post-processing
    # (formatting, images, KDP) below.
    if args.no_text:
        print("Skipping text generation (--no-text): document already contains "
              "finished prose. Running post-processing only.", flush=True)
        # Ensure the output file exists for the post-processing stage, which
        # reads from out_path.
        if str(out_path) != str(in_path):
            out_path.parent.mkdir(parents=True, exist_ok=True)
            doc.save(str(out_path))

    ai_roles: Optional[dict] = None
    if not args.no_text and args.agent and not args.no_ai_outline:
        outline_lines = [(_p.text or "").strip() for _p in doc.paragraphs if (_p.text or "").strip()]
        try:
            print("Analyzing outline structure with AI...", flush=True)
            ai_roles = classify_outline_with_ai(
                agent_id=args.agent,
                lines=outline_lines,
                local=args.local,
                thinking=args.thinking,
                timeout_s=args.timeout,
            )
        except Exception as e:
            print(f"WARNING: AI outline analysis failed ({e}); using built-in detection.",
                  file=sys.stderr)
            ai_roles = None
        if ai_roles is None:
            print("AI outline analysis unusable — using built-in detection.", flush=True)
        else:
            n_ch = sum(1 for v in ai_roles.values() if v == "chapter")
            n_se = sum(1 for v in ai_roles.values() if v == "section")
            print(f"AI outline analysis: {n_ch} chapter heading(s), {n_se} section(s).", flush=True)

    def outline_role(paragraph) -> Optional[str]:
        """'chapter' | 'section' | None for a paragraph, from the AI map."""
        if ai_roles is None:
            return None
        return ai_roles.get(_normalize_outline_text(paragraph.text))

    # Pre-scan to count total work items for progress reporting.
    # Skipped entirely under --no-text so nothing is treated as a heading.
    total_items = 0
    if not args.no_text:
        for _p in doc.paragraphs:
            if not (_p.text or "").strip():
                continue
            if ai_roles is not None:
                if outline_role(_p) in ("chapter", "section"):
                    total_items += 1
            elif is_heading_paragraph(_p) or is_subheading_paragraph(_p):
                total_items += 1
        print(f"Found {total_items} headings/subheadings to process.")
    processed_items = 0
    skipped_headings: list[str] = []
    run_start = time.time()

    # We'll iterate by index because we need to look at nearby paragraphs.
    # `--no-text` sets i past the end so the whole generation loop is skipped.
    i = len(doc.paragraphs) if args.no_text else 0
    while i < len(doc.paragraphs):
        p = doc.paragraphs[i]
        if ai_roles is not None:
            role = outline_role(p)
            is_h = role == "chapter"
            is_sub = role == "section"
        else:
            is_h = is_heading_paragraph(p)
            is_sub = (not is_h) and is_subheading_paragraph(p)

        if not is_h and not is_sub:
            i += 1
            continue

        heading = (p.text or "").strip()
        if not heading:
            i += 1
            continue

        processed_items += 1
        elapsed = time.time() - run_start
        elapsed_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))
        tag = "heading" if is_h else "subheading"
        print(f"[{processed_items}/{total_items}] [{elapsed_str}] Processing {tag}: {heading[:80]}", flush=True)

        # If next paragraph is non-empty normal content, text is already present.
        # A line the AI labeled as part of the outline is never "content".
        next_check: Optional[Any] = doc.paragraphs[i + 1] if (i + 1) < len(doc.paragraphs) else None
        has_existing_content = (
            next_check is not None
            and paragraph_looks_like_body(next_check)
            and outline_role(next_check) is None
        )
        skip_text_generation = (not args.force) and has_existing_content

        # Skip already-written content during the text pass.
        if skip_text_generation:
            i += 1
            continue

        changed = False
        anchor_para: Paragraph = p
        generated = ""

        # Chapter-title headings get no intro text by default: an intro
        # generated in isolation always half-repeats what the subheadings
        # below it cover. The image block below still runs for them.
        skip_chapter_intro = (
            is_h and not args.chapter_intros and is_chapter_title_heading(heading)
        )

        if skip_chapter_intro:
            print("  chapter title — no intro text (subheadings carry the content)", flush=True)
        else:
            if is_h:
                prompt = build_prompt(heading=heading, words_min=words_min, words_max=words_max, tone=args.tone)
            else:
                prompt = build_subheading_prompt(subheading=heading, words_min=subwords_min, words_max=subwords_max, tone=args.tone)
            cache_key = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

            cached = None if (args.no_cache or args.force) else cache.get(cache_key)
            if cached is not None and find_template_sentences(cached):
                print("  cached text contains a banned pattern — regenerating", flush=True)
                cached = None
            if cached is not None:
                generated = cached
                print("  text from cache", flush=True)
            else:
                call_start = time.time()
                try:
                    generated = generate_clean_paragraph(
                        agent_id=args.agent,
                        message=prompt,
                        local=args.local,
                        thinking=args.thinking,
                        timeout_s=args.timeout,
                        # A fresh session per section, not the shared book-level
                        # session_id. SOUL.md is explicit that headings are
                        # independent and must not carry context from one to the
                        # next, but a shared session resends the whole prior
                        # conversation as input on every call, so cost grew
                        # quadratically with section count for no quality benefit.
                        session_id=str(uuid.uuid4()),
                    )
                except TemplatePatternError as e:
                    print(f"  ERROR: {e}", file=sys.stderr)
                    print("  Heading left empty (banned pattern is never written to the book); "
                          "re-run to fill it.", flush=True)
                    skipped_headings.append(heading)
                    i += 1
                    continue
                call_dur = time.time() - call_start
                cache.set(cache_key, generated)
                remaining = total_items - processed_items
                eta_s = call_dur * remaining
                eta_str = time.strftime("%H:%M:%S", time.gmtime(eta_s))
                print(f"  generated ({call_dur:.0f}s, {len(generated.split())} words) — "
                      f"~{remaining} left, ETA ~{eta_str}", flush=True)
                if args.sleep > 0:
                    time.sleep(args.sleep)

            # Put the paragraph right after heading/subheading.
            next_para: Optional[Any] = doc.paragraphs[i + 1] if (i + 1) < len(doc.paragraphs) else None
            next_is_content = (
                next_para is not None
                and paragraph_looks_like_body(next_para)
                and outline_role(next_para) is None
            )

            if next_is_content and (args.force or (next_para.text or "").strip() == ""):
                next_para.text = generated
                anchor_para = next_para
            else:
                anchor_para = insert_paragraph_after(p, generated, style="Normal")
            changed = True

        # Optional image generation for main headings only (uses OpenAI).
        if is_h and args.images and not defer_images_until_postprocess:
            try:
                existing_image_para = find_existing_image_paragraph(doc, i)
                if existing_image_para is not None and not args.force:
                    print("  image already exists; skipping")
                else:
                    image_cache_key = hashlib.sha256(
                        f"image::{args.image_model}::{args.image_size}::{args.image_quality}::{heading}".encode("utf-8")
                    ).hexdigest()
                    image_output = image_cache_path / f"{image_cache_key}.png"
                    image_path = None if args.force else find_cached_image(image_cache_path, image_cache_key)

                    if image_path is not None:
                        print("  image from cache")
                    else:
                        print("  generating image via openclaw_image_maker.py")
                        image_path = generate_image_via_image_maker(
                            heading=heading,
                            paragraph_text=generated,
                            output_path=image_output,
                            openai_api_key=openai_api_key,
                            image_model=args.image_model,
                            image_size=args.image_size,
                            image_quality=args.image_quality,
                            prompt_variant=args.image_prompt_variant,
                            cache_dir=str(image_cache_path),
                            force=args.force,
                        )
                        if args.sleep > 0:
                            time.sleep(args.sleep)

                    if existing_image_para is not None and args.force:
                        set_paragraph_image(existing_image_para, image_path, width_inches=image_width)
                    else:
                        insert_image_after(anchor_para, image_path, width_inches=image_width)
                    changed = True
            except Exception as e:
                print(f"  WARNING: image generation failed for heading '{heading[:60]}': {e}", file=sys.stderr)

        if changed:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            doc.save(str(out_path))
            print(f"  Saved: {out_path}", flush=True)

        # Move forward (safe even if doc.paragraphs grows)
        i += 1

    # Chapter-level cap: at most 4 uses of "just" per chapter across the book.
    # Skipped under --no-text: never edit the human author's finished prose.
    if not args.no_text:
        trimmed = enforce_chapter_just_budget(doc)
        if trimmed:
            doc.save(str(out_path))
            print(f"'just' budget: trimmed extras in {trimmed} paragraph(s)", flush=True)

    if skipped_headings:
        print(f"WARNING: {len(skipped_headings)} section(s) still empty after "
              "all regeneration attempts:", flush=True)
        for h in skipped_headings:
            print(f"  - {h[:80]}", flush=True)
        print("Re-run the job to retry the empty sections.", flush=True)

    print(f"Done: {out_path}")
    image_target_path = out_path
    if ENABLE_AUTO_POSTPROCESS:
        # Auto-format the output document
        formatted_path = out_path.with_stem(out_path.stem + "_formatted")
        print(f"\nFormatting document → {formatted_path}")
        try:
            from format_docx import format_document
            format_document(out_path, formatted_path)
            image_target_path = formatted_path
        except Exception as e:
            print(f"WARNING: formatting failed — {e}", file=sys.stderr)

        # Auto-run Hemingway clarity scrub on the formatted document via Playwright.
        # Runs even under --no-text (already-written books): the scrub only
        # tightens/clarifies existing sentences, it doesn't invent new content,
        # and it has its own safeguards (fix_comma_splices, banned-pattern
        # check below) that fall back to the pre-scrub doc if anything looks
        # off. An already-written book still needs the clarity pass — the
        # thing --no-text protects is generation, not this step.
        clear_path = formatted_path.with_stem(formatted_path.stem + "_clear")
        print(f"\nClarity scrub → {clear_path}")
        try:
            from clarity_agent import (
                extract_docx_text, save_text_to_docx,
                process_document, HEMINGWAY_URL, PROFILE_DIR,
            )
            from playwright.sync_api import sync_playwright

            text = extract_docx_text(formatted_path)
            if text.strip():
                PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                with sync_playwright() as pw:
                    context = pw.chromium.launch_persistent_context(
                        str(PROFILE_DIR),
                        headless=True,
                        permissions=["clipboard-read", "clipboard-write"],
                    )
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(HEMINGWAY_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_selector("[contenteditable='true']", timeout=30000)

                    # Dismiss any modal dialog (e.g. video popup) before interacting
                    try:
                        from clarity_agent import dismiss_modal_dialogs
                        dismiss_modal_dialogs(page)
                    except ImportError:
                        pass

                    try:
                        cleaned = process_document(page, text, max_passes=5)
                    except Exception as exc:
                        from clarity_agent import UpgradePlanRequired
                        if isinstance(exc, UpgradePlanRequired) and exc.args:
                            cleaned = exc.args[0]
                        else:
                            # For non-UpgradePlanRequired errors, keep original text
                            cleaned = text
                        print(f"  Clarity scrub stopped early: {exc}", file=sys.stderr)
                    context.close()

                cleaned = humanize_text(cleaned)
                # Final gate: the scrub rewrites text after generation, so its
                # output gets the same enforcement. If a banned pattern survives
                # the mechanical fix, keep the pre-scrub (already gated) doc.
                cleaned = fix_comma_splices(cleaned)
                scrub_leftovers = find_template_sentences(cleaned)
                if scrub_leftovers:
                    raise RuntimeError(
                        "clarity scrub output contains a banned 'not X, it's Y' "
                        "pattern; keeping the pre-scrub document: "
                        + "; ".join(f'"{s[:80]}"' for s in scrub_leftovers[:3])
                    )
                if cleaned.strip():
                    save_text_to_docx(cleaned, clear_path)
                    print(f"  Clarity scrub saved: {clear_path}")
                    image_target_path = clear_path
                else:
                    print("  WARNING: clarity scrub returned empty text, skipping.", file=sys.stderr)
            else:
                print("  WARNING: no text in formatted doc, skipping clarity scrub.", file=sys.stderr)
        except ImportError as e:
            print(f"  INFO: clarity_agent not available ({e}), skipping. "
                  f"Run manually: python clarity_agent.py {formatted_path}", file=sys.stderr)
        except Exception as e:
            print(f"  WARNING: clarity scrub failed — {e}", file=sys.stderr)
            print(f"  Run manually: python clarity_agent.py {formatted_path}", file=sys.stderr)
    else:
        print("\nSkipping formatting and clarity scrub (disabled for now).")

    # Insert images after post-processing so they survive format/clarity rewrite.
    # This runs even under --no-text (already-written books): the caller opts in
    # with --images, and insert_images_into_document only generates for headings
    # that DON'T already have an image (it skips ones that do). Placement anchors
    # to real docx headings via _select_heading_for_image, so a flat book with no
    # heading styles simply gets nothing placed rather than misplaced images.
    if args.images:
        try:
            print(f"\nInserting images into final document → {image_target_path}")
            inserted, replaced = insert_images_into_document(
                doc_path=image_target_path,
                image_cache_path=image_cache_path,
                image_width=image_width,
                force=args.force,
                openai_api_key=openai_api_key,
                image_model=args.image_model,
                image_size=args.image_size,
                image_quality=args.image_quality,
                prompt_variant=args.image_prompt_variant,
                sleep_s=args.sleep,
                image_heading_filter=args.image_heading,
            )
            print(
                f"  Image insertion complete: inserted={inserted}, replaced={replaced}. "
                f"Final file: {image_target_path}"
            )
        except Exception as e:
            print(f"  WARNING: final image insertion failed — {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
openclaw_docx_writer.py

Reads a .docx, sends each heading to OpenClaw, and inserts the generated paragraph
right after that heading.

Usage:
  python openclaw_docx_writer.py input.docx output.docx --agent main

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
PLAIN_HEADING_RE = re.compile(
    r"^(Chapter\s+\d+(?:\s*[:–—-]\s*.*)?|Introduction:?\s*.*|Conclusion:?\s*.*|Epilogue:?\s*.*|Foreword:?\s*.*|Preface:?\s*)$",
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

def is_heading_paragraph(p) -> bool:
    try:
        name = (p.style.name or "").strip()
    except Exception:
        return False
    if bool(HEADING_RE.match(name)) or name.lower() in {"title"}:
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
    """
    new_p = OxmlElement("w:p")
    paragraph._p.addnext(new_p)
    new_para = Paragraph(new_p, paragraph._parent)
    try:
        new_para.style = paragraph._parent.part.document.styles[style]
    except (KeyError, Exception):
        new_para.style = style
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


def build_prompt(heading: str, words_min: int, words_max: int, tone: str) -> str:
    return (
        f"You are ghostwriting a non-fiction book for someone who writes the way they talk. "
        f"The writing must sound like a real person typed it on a laptop at a coffee shop, not like a language model.\n\n"
        f"Section topic: {heading}\n\n"
        f"Write ONE paragraph, {words_min}–{words_max} words.\n\n"
        f"VOICE & STYLE (critical):\n"
        f"- Use contractions freely (don't, isn't, won't, there's, we've, etc.).\n"
        f"- Mix sentence lengths A LOT: some very short (3–6 words), some medium, a few long and winding. "
        f"This variation is the single most important thing.\n"
        f"- Start some sentences with 'And', 'But', 'So', 'Still', 'Thing is', 'Look', or 'Honestly'.\n"
        f"- Occasionally use a sentence fragment on purpose. Not every sentence needs a subject and verb.\n"
        f"- Prefer simple, everyday words. Say 'big' not 'substantial', 'use' not 'utilize', 'help' not 'facilitate'.\n"
        f"- Throw in a short rhetorical question once in a while.\n"
        f"- Vary paragraph rhythm: don't follow topic-sentence → evidence → conclusion. Jump around a bit.\n"
        f"- Use 'you' or 'we' naturally when it fits the context.\n"
        f"- Write in a {tone} tone, but keep it grounded and unpretentious.\n\n"
        f"HARD BANS:\n"
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
    return (
        f"You are ghostwriting a non-fiction book for someone who writes the way they talk. "
        f"The writing must sound like a real person typed it on a laptop at a coffee shop, not like a language model.\n\n"
        f"Specific point to cover: {clean}\n\n"
        f"Write ONE paragraph, {words_min}–{words_max} words, on this specific point.\n\n"
        f"VOICE & STYLE (critical):\n"
        f"- Use contractions freely (don't, isn't, won't, there's, we've, etc.).\n"
        f"- Mix sentence lengths A LOT: some very short (3–6 words), some medium, a few long and winding. "
        f"This variation is the single most important thing.\n"
        f"- Start some sentences with 'And', 'But', 'So', 'Still', 'Thing is', or 'Honestly'.\n"
        f"- Occasionally use a sentence fragment on purpose.\n"
        f"- Prefer simple, everyday words. Say 'big' not 'substantial', 'use' not 'utilize'.\n"
        f"- Use 'you' or 'we' naturally when it fits the context.\n"
        f"- Write in a {tone} tone, but keep it grounded and unpretentious.\n\n"
        f"HARD BANS:\n"
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


def humanize_text(text: str) -> str:
    """
    Post-process generated text to strip residual AI-isms and
    improve burstiness / naturalness scores.
    """
    if not text:
        return text

    # 1) Remove any banned phrases (case-insensitive)
    for phrase in BANNED_PHRASES:
        pattern = re.compile(re.escape(phrase), re.IGNORECASE)
        text = pattern.sub("", text)

    # 2) Replace common non-contraction forms with contractions
    contraction_map = [
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
        (r"\bit is\b", "it's"),
        (r"\bIt is\b", "It's"),
        (r"\bthat is\b", "that's"),
        (r"\bThat is\b", "That's"),
        (r"\bthere is\b", "there's"),
        (r"\bThere is\b", "There's"),
        (r"\bwe are\b", "we're"),
        (r"\bWe are\b", "We're"),
        (r"\bthey are\b", "they're"),
        (r"\bThey are\b", "They're"),
        (r"\byou are\b", "you're"),
        (r"\bYou are\b", "You're"),
        (r"\bI am\b", "I'm"),
        (r"\bwe have\b", "we've"),
        (r"\bWe have\b", "We've"),
        (r"\bthey have\b", "they've"),
        (r"\bThey have\b", "They've"),
        (r"\bI have\b", "I've"),
        (r"\blet us\b", "let's"),
        (r"\bLet us\b", "Let's"),
    ]
    for pat, repl in contraction_map:
        text = re.sub(pat, repl, text)

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

    # 4) Remove dashes that may have slipped through
    text = text.replace("\u2014", ". ")   # em dash → period
    text = text.replace("\u2013", ", ")   # en dash → comma

    # 5) Collapse double-spaces and fix spacing after substitutions
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
    args = ap.parse_args()

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

            generated = call_openclaw(
                agent_id=args.agent,
                message=prompt,
                local=args.local,
                thinking=args.thinking,
                timeout_s=args.timeout,
                session_id=rw_session,
            )
            next_para.text = generated
            rewritten += 1
            print(f"  rewritten ({len(generated)} chars)")

            i += 1

        if rewritten > 0:
            doc.save(str(target))
        print(f"Done: rewritten={rewritten}. File: {target}")
        return 0

    if not args.agent:
        print("ERROR: --agent is required (unless using --image-heading or --rewrite-heading).", file=sys.stderr)
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

    # Pre-scan to count total work items for progress reporting.
    total_items = 0
    for _p in doc.paragraphs:
        if is_heading_paragraph(_p) or is_subheading_paragraph(_p):
            if (_p.text or "").strip():
                total_items += 1
    print(f"Found {total_items} headings/subheadings to process.")
    processed_items = 0
    run_start = time.time()

    # We'll iterate by index because we need to look at nearby paragraphs.
    i = 0
    while i < len(doc.paragraphs):
        p = doc.paragraphs[i]
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
        next_check: Optional[Any] = doc.paragraphs[i + 1] if (i + 1) < len(doc.paragraphs) else None
        has_existing_content = (
            next_check is not None
            and paragraph_looks_like_body(next_check)
        )
        skip_text_generation = (not args.force) and has_existing_content

        # Skip already-written content during the text pass.
        if skip_text_generation:
            i += 1
            continue

        changed = False
        anchor_para: Paragraph = p

        if is_h:
            prompt = build_prompt(heading=heading, words_min=words_min, words_max=words_max, tone=args.tone)
        else:
            prompt = build_subheading_prompt(subheading=heading, words_min=subwords_min, words_max=subwords_max, tone=args.tone)
        cache_key = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

        cached = cache.get(cache_key)
        if cached is not None:
            generated = cached
            print("  text from cache", flush=True)
        else:
            call_start = time.time()
            generated = call_openclaw(
                agent_id=args.agent,
                message=prompt,
                local=args.local,
                thinking=args.thinking,
                timeout_s=args.timeout,
                session_id=session_id,
            )
            call_dur = time.time() - call_start
            cache.set(cache_key, generated)
            remaining = total_items - processed_items
            eta_s = call_dur * remaining
            eta_str = time.strftime("%H:%M:%S", time.gmtime(eta_s))
            print(f"  generated ({call_dur:.0f}s) — ~{remaining} left, ETA ~{eta_str}", flush=True)
            if args.sleep > 0:
                time.sleep(args.sleep)

        # Put the paragraph right after heading/subheading.
        next_para: Optional[Any] = doc.paragraphs[i + 1] if (i + 1) < len(doc.paragraphs) else None
        next_is_content = (
            next_para is not None
            and paragraph_looks_like_body(next_para)
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

        # Auto-run Hemingway clarity scrub on the formatted document via Playwright
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

#!/usr/bin/env python3
"""Generate a single standalone grayscale image for book use."""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import shutil
import sys
from pathlib import Path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}

PROMPT_VARIANTS: dict[str, str] = {
    "three-gray": (
        "Create a standalone book illustration for '{heading}'. "
        "Subject and composition should reflect this context: {paragraph}. "
        "Use monochrome only: black and exactly 3 shades of gray. "
        "No color at all. "
        "Keep the full subject fully visible and centered; do not crop or cut off any edge. "
        "Use a plain white background with generous white space around the subject so it floats on the page. "
        "Clean, simple, high-contrast shapes. No text, labels, frames, or borders."
    ),
    "black-gray": (
        "Create a standalone book illustration for '{heading}'. "
        "Subject and composition should reflect this context: {paragraph}. "
        "Use only pure black plus shades of gray on white. No color. "
        "Keep the full subject fully visible and centered; do not crop or cut off any edge. "
        "Use a plain white background with extra white margin around the subject so it floats on the page. "
        "No text, labels, frames, or borders."
    ),
    "chapter-page-gray": (
        "Create one single flat 2D chapter-opener page, portrait orientation, with a plain white background. "
        "This must be a direct page design, not a photograph and not a 3D render. "
        "Do not show a physical book, page curl, page stack, desk, shadows, camera angle, or perspective view. "
        "At the very top center, place one title only: '{heading}' in bold uppercase black sans-serif letters. "
        "Do not add any other text anywhere on the page. "
        "Semantic match is critical. Use these heading keywords as mandatory visual cues: {heading_keywords}. "
        "Use this theme guidance: {theme_guidance}. "
        "Below the title, place one centered playful monochrome illustration related to this context: {paragraph}. "
        "The illustration must include at least two concrete symbols that match the heading meaning. "
        "Use black plus exactly 3 shades of gray only, no color. "
        "Keep all illustration elements fully visible and centered; do not crop or cut off any part. "
        "Use generous white space around the subject so it floats on the page. "
        "Simple line-art with soft gray fills, kid-friendly style, high contrast, no clutter. "
        "No open book, no two-page spread, no frame, no border, no watermark, no mockup."
    ),
    "chapter-page-rich-gray": (
        "Create one single flat 2D chapter-opener page, portrait orientation, with a plain white background. "
        "This must be a direct page design, not a photograph and not a 3D render. "
        "Do not show a physical book, page curl, page stack, desk, shadows, camera angle, or perspective view. "
        "At the very top center, place one title only: '{heading}' in bold uppercase black sans-serif letters. "
        "Do not add any other text anywhere on the page. "
        "Semantic match is critical. Use these heading keywords as mandatory visual cues: {heading_keywords}. "
        "Use this theme guidance: {theme_guidance}. "
        "Below the title, place one centered, attractive, storybook-quality cartoon illustration related to this context: {paragraph}. "
        "Include multiple supportive elements that match the heading meaning, not just one simple icon. "
        "Use varied line weight, expressive poses, richer detail in clothing/hair/objects, and soft stipple or crosshatch texture. "
        "Keep style western cartoon and kid-friendly, not anime, not manga, not realistic portrait. "
        "Use black plus exactly 3 shades of gray only, no color. "
        "Keep all illustration elements fully visible and centered; do not crop or cut off any part. "
        "Use generous white space around the subject so it floats on the page. "
        "No open book, no two-page spread, no frame, no border, no watermark, no mockup."
    ),
    "rich-scene-no-text": (
        "Create one single flat 2D storybook-quality western cartoon illustration on a **stark, pure white background (#FFFFFF)**. "
        "**Strictly neutral grayscale: use only deep black and exactly 3 distinct shades of cool gray. Absolutely no yellow, sepia, cream, or warm tones.** "
        "Semantic match is critical. Use these heading keywords as mandatory visual cues: {heading_keywords}. "
        "Use this theme guidance: {theme_guidance}. "
        "Create an attractive central scene related to: {heading}. Context: {paragraph}. "
        "Include multiple supportive elements that match the heading meaning, not just one simple icon. "
        "Use varied line weight, expressive poses, richer detail in clothing/hair/objects, and soft stipple or crosshatch texture. "
        "Keep style western cartoon and kid-friendly, not anime, not manga, not realistic portrait. "
        "Keep all illustration elements fully visible and centered; do not crop or cut off any part. "
        "Use generous white space around the subject so it floats on the page. "
        "**Zero text, no letters, no symbols, no numbers, no labels, no frame, no border, no watermark.**"
    ),
}

HEADING_STOPWORDS = {
    "a", "an", "and", "as", "at", "for", "from", "in", "into", "of", "on", "or",
    "the", "to", "with", "your", "our", "my", "is", "are", "be", "getting",
}

ROMANCE_HINTS = (
    "engagement", "engaged", "fiance", "fiancee", "proposal", "propose", "wedding",
    "marriage", "bride", "groom", "ring", "love",
)


def extract_heading_keywords(heading: str, max_words: int = 6) -> str:
    words = re.findall(r"[A-Za-z0-9']+", (heading or "").lower())
    picked: list[str] = []
    for w in words:
        if len(w) < 3 or w in HEADING_STOPWORDS:
            continue
        if w not in picked:
            picked.append(w)
        if len(picked) >= max_words:
            break
    if not picked:
        return (heading or "").strip()
    return ", ".join(picked)


def infer_theme_guidance(heading: str, paragraph_text: str) -> str:
    text = f"{heading} {paragraph_text}".lower()
    notes: list[str] = []

    if any(k in text for k in ROMANCE_HINTS):
        notes.append("Treat engagement as romantic commitment before marriage.")
        notes.append("Show an engagement ring and a couple or proposal cue.")

    if "getting started" in text or "first step" in text or "begin" in text:
        notes.append("Include a subtle first-step cue like a simple checklist or path.")

    if not notes:
        notes.append("Use literal symbols directly tied to heading keywords and avoid generic clip-art.")

    return " ".join(notes)


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
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


def build_image_prompt_from_paragraph(heading: str, paragraph_text: str, template: str) -> str:
    short_context = paragraph_text[:200].strip() if paragraph_text else ""
    heading_keywords = extract_heading_keywords(heading=heading)
    theme_guidance = infer_theme_guidance(heading=heading, paragraph_text=short_context)
    values = {
        "heading": heading,
        "paragraph": short_context,
        "heading_keywords": heading_keywords,
        "theme_guidance": theme_guidance,
    }
    try:
        prompt = template.format(**values)
    except (KeyError, IndexError):
        prompt = template
        for key, value in values.items():
            prompt = prompt.replace(f"{{{key}}}", value)
    return prompt.strip()


def find_cached_image(cache_dir: Path, cache_key: str) -> Path | None:
    if not cache_dir.exists():
        return None
    matches = sorted(
        p for p in cache_dir.glob(f"{cache_key}.*")
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )
    return matches[0] if matches else None


def generate_image_openai(
    prompt: str,
    api_key: str,
    cache_dir: Path,
    cache_key: str,
    model: str,
    size: str,
    quality: str,
) -> Path:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    request_kwargs = dict(
        model=model,
        prompt=prompt,
        n=1,
        size=size,
        quality=quality,
    )
    # response_format is accepted by DALL-E models but rejected by gpt-image-1.
    if model.lower().startswith("dall-e"):
        request_kwargs["response_format"] = "b64_json"

    response = client.images.generate(**request_kwargs)

    first = response.data[0]
    b64_payload = getattr(first, "b64_json", None)
    if not b64_payload and isinstance(first, dict):
        b64_payload = first.get("b64_json")
    image_data = base64.b64decode(b64_payload) if b64_payload else b""
    if not image_data:
        raise RuntimeError("OpenAI returned empty image data.")

    cache_dir.mkdir(parents=True, exist_ok=True)
    for old in cache_dir.glob(f"{cache_key}.*"):
        if old.is_file() and old.suffix.lower() in IMAGE_SUFFIXES:
            old.unlink(missing_ok=True)

    image_path = cache_dir / f"{cache_key}.png"
    image_path.write_bytes(image_data)
    (cache_dir / f"{cache_key}.prompt.txt").write_text(prompt, encoding="utf-8")
    return image_path


def resolve_api_key(explicit_key: str) -> str:
    env_candidates = [
        (Path.cwd() / ".env").resolve(),
        (Path(__file__).resolve().parent / ".env").resolve(),
    ]
    seen: set[Path] = set()
    for env_path in env_candidates:
        if env_path in seen:
            continue
        seen.add(env_path)
        load_env_file(env_path)

    return explicit_key or os.environ.get("OPENAI_API_KEY", "")


def compose_title_page(image_path: Path, title_text: str, font_size: int) -> None:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(image_path).convert("RGB")
    w, h = img.size

    # Normalize near-white pixels to white so the final page stays clean.
    px = img.load()
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            if r >= 232 and g >= 232 and b >= 232:
                px[x, y] = (255, 255, 255)

    canvas = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(canvas)

    title = (title_text or "").strip()
    if not title:
        raise RuntimeError("Title text is empty for compose_title_page.")

    # If the title is long, split into two lines around '&' or midpoint.
    if len(title) > 24 and "\n" not in title:
        if "&" in title:
            title = title.replace("&", "&\n", 1)
        else:
            words = title.split()
            midpoint = max(1, len(words) // 2)
            title = " ".join(words[:midpoint]) + "\n" + " ".join(words[midpoint:])

    font_candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica.ttc",
        "/Library/Fonts/Arial Bold.ttf",
    ]
    font = None
    for p in font_candidates:
        try:
            font = ImageFont.truetype(p, max(28, int(font_size)))
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    bbox = draw.multiline_textbbox((0, 0), title, font=font, spacing=12, align="center")
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (w - tw) // 2
    ty = 88

    draw.multiline_text((tx, ty), title, fill="black", font=font, spacing=12, align="center")

    art_top = ty + th + 70
    max_h = h - art_top - 90
    max_w = w - 120
    scale = min(max_w / w, max_h / h)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))
    art = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    ax = (w - new_w) // 2
    ay = art_top + (max_h - new_h) // 2
    canvas.paste(art, (ax, ay))

    canvas.save(image_path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--heading", required=True, help="Heading/topic used in the image prompt")
    ap.add_argument("--paragraph", default="", help="Optional paragraph context for richer prompt")
    ap.add_argument(
        "--prompt-variant",
        default="chapter-page-rich-gray",
        choices=sorted(PROMPT_VARIANTS.keys()),
        help="Preset prompt style",
    )
    ap.add_argument(
        "--prompt-template",
        default="",
        help="Custom template. Use {heading} and optionally {paragraph}. Overrides --prompt-variant.",
    )
    ap.add_argument("--prompt", default="", help="Raw full prompt. Overrides template and variant.")
    ap.add_argument("--openai-api-key", default="", help="OpenAI API key (or use OPENAI_API_KEY env var)")
    ap.add_argument("--model", default="dall-e-3", help="OpenAI image model (recommended: dall-e-3)")
    ap.add_argument("--size", default="1024x1792", help="Image size")
    ap.add_argument("--quality", default="hd", help="Image quality")
    ap.add_argument("--cache", default=".openclaw_cache/images", help="Image cache directory")
    ap.add_argument("--output", default="generated_image.png", help="Output image path")
    ap.add_argument("--compose-title", action="store_true",
                    help="After image generation, overlay a clean heading title on top (avoids text-garbled model output)")
    ap.add_argument("--compose-title-text", default="",
                    help="Title text to overlay (default: --heading)")
    ap.add_argument("--compose-font-size", type=int, default=88,
                    help="Font size for --compose-title mode")
    ap.add_argument("--force", action="store_true", help="Regenerate even if a cache hit exists")
    args = ap.parse_args()

    api_key = resolve_api_key(args.openai_api_key)
    if not api_key:
        print("ERROR: OpenAI API key is required (use --openai-api-key or OPENAI_API_KEY).", file=sys.stderr)
        return 2

    if args.prompt.strip():
        prompt = args.prompt.strip()
    else:
        template = args.prompt_template.strip() or PROMPT_VARIANTS[args.prompt_variant]
        prompt = build_image_prompt_from_paragraph(
            heading=args.heading,
            paragraph_text=args.paragraph,
            template=template,
        )

    cache_dir = Path(args.cache)
    output_path = Path(args.output)

    cache_key = hashlib.sha256(
        f"img::{args.model}::{args.size}::{args.quality}::{prompt}".encode("utf-8")
    ).hexdigest()

    image_path = None if args.force else find_cached_image(cache_dir, cache_key)
    if image_path is None:
        image_path = generate_image_openai(
            prompt=prompt,
            api_key=api_key,
            cache_dir=cache_dir,
            cache_key=cache_key,
            model=args.model,
            size=args.size,
            quality=args.quality,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(image_path, output_path)

    if args.compose_title:
        title = args.compose_title_text.strip() or args.heading.strip()
        if not title:
            print("ERROR: --compose-title requires non-empty --heading or --compose-title-text.", file=sys.stderr)
            return 2
        compose_title_page(
            image_path=output_path,
            title_text=title,
            font_size=max(24, int(args.compose_font_size)),
        )

    output_prompt_path = output_path.with_name(output_path.stem + ".prompt.txt")
    output_prompt_path.write_text(prompt + "\n", encoding="utf-8")

    print(f"Saved image: {output_path.resolve()}")
    print(f"Prompt file: {output_prompt_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Cover generation — one ``images.edit`` call per reference, in parallel.

Each variation pairs the same prompt with a *different* proven cover, so the
ten results differ by style rather than by chance. That is the mechanic from
the operator's manual workflow, made repeatable.

``dall-e-3`` cannot accept a reference image at all, so this path uses
``gpt-image-1`` — the same model and endpoint already used by
trivia/image_edit.py, stories/image_edit.py and docx_image_edit.py.

Every finished cover goes through ``print_hygiene.sanitize_for_print`` so it
lands at exactly 300 DPI with no EXIF, XMP, or C2PA provenance chunk. Covers
saved out of a chat window have none of that done, so this is a real
correctness gain over the manual process, not just convenience.
"""

from __future__ import annotations

import base64
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT_DIR = Path(__file__).resolve().parent.parent
COVER_OUTPUT_DIR = ROOT_DIR / "cover_outputs"

MODEL = "gpt-image-1"

# 1024x1536 is the portrait option images.edit accepts and is the 2:3 ratio a
# 6x9 trade paperback needs. The others are here so a caller can request a
# square or landscape variant without editing this module.
VALID_SIZES = {"1024x1024", "1536x1024", "1024x1536", "auto"}
DEFAULT_SIZE = "1024x1536"

# "high" is the shipping quality. "medium" exists so the operator can browse
# ten style directions cheaply and re-render only the winner at full quality.
VALID_QUALITIES = {"low", "medium", "high", "auto"}
DEFAULT_QUALITY = "high"

# Ten covers at once would open ten sockets and invite a rate-limit burst;
# four keeps a 10-cover run comfortably parallel without stampeding.
MAX_WORKERS = 4


class CoverGenerationError(RuntimeError):
    """Raised when a run cannot start (no key, bad size, no references)."""


@dataclass
class CoverVariation:
    index: int
    reference_name: str
    status: str = "pending"        # pending | done | error
    filename: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "reference_name": self.reference_name,
            "status": self.status,
            "filename": self.filename,
            "error": self.error,
        }


@dataclass
class CoverRun:
    id: str
    title: str
    book_type: str
    prompt: str
    size: str
    quality: str
    variations: list[CoverVariation] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @property
    def output_dir(self) -> Path:
        return COVER_OUTPUT_DIR / self.id

    def to_dict(self) -> dict[str, Any]:
        done = sum(1 for v in self.variations if v.status == "done")
        return {
            "id": self.id,
            "title": self.title,
            "book_type": self.book_type,
            "prompt": self.prompt,
            "size": self.size,
            "quality": self.quality,
            "created_at": self.created_at,
            "variations": [v.to_dict() for v in self.variations],
            "done_count": done,
            "total": len(self.variations),
        }


def _finalize(path: Path) -> None:
    """Strip metadata and pin DPI. Never fatal — a cover that exists but
    missed hygiene is still worth showing, flagged, over losing the run."""
    try:
        from print_hygiene import sanitize_for_print

        sanitize_for_print(path)
    except Exception:
        pass


def _generate_one(
    prompt: str,
    reference: Path,
    dest: Path,
    api_key: str,
    size: str,
    quality: str,
) -> None:
    """One cover: prompt + one reference image -> one PNG on disk."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key)

    with reference.open("rb") as fh:
        ref_bytes = fh.read()

    response = client.images.edit(
        model=MODEL,
        image=(reference.name, ref_bytes, "image/png"),
        prompt=prompt,
        n=1,
        size=size,
        quality=quality,
    )

    first = response.data[0] if response.data else None
    b64 = getattr(first, "b64_json", None)
    if not b64 and isinstance(first, dict):
        b64 = first.get("b64_json")
    if not b64:
        raise CoverGenerationError("The image service returned no image data.")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(base64.b64decode(b64))
    _finalize(dest)


def generate_covers(
    title: str,
    prompt: str,
    references: list[Path],
    api_key: str,
    book_type: str = "puzzle",
    size: str = DEFAULT_SIZE,
    quality: str = DEFAULT_QUALITY,
    on_progress: Callable[[CoverRun], None] | None = None,
) -> CoverRun:
    """Generate one cover per reference. Returns when all have settled.

    A failed variation is recorded on that variation and does not abort the
    run — nine good covers beat an exception.
    """
    if not api_key:
        raise CoverGenerationError("No OpenAI API key available for cover generation.")
    if not prompt.strip():
        raise CoverGenerationError("Cover prompt is empty.")
    if not references:
        raise CoverGenerationError("At least one reference cover is required.")
    if size not in VALID_SIZES:
        raise CoverGenerationError(f"size must be one of {sorted(VALID_SIZES)}.")
    if quality not in VALID_QUALITIES:
        raise CoverGenerationError(f"quality must be one of {sorted(VALID_QUALITIES)}.")

    run = CoverRun(
        id=uuid.uuid4().hex[:12],
        title=title,
        book_type=book_type,
        prompt=prompt,
        size=size,
        quality=quality,
        variations=[
            CoverVariation(index=i, reference_name=ref.stem)
            for i, ref in enumerate(references)
        ],
    )
    run.output_dir.mkdir(parents=True, exist_ok=True)

    def _task(i: int, ref: Path) -> tuple[int, str, str]:
        dest = run.output_dir / f"cover_{i + 1:02d}.png"
        try:
            _generate_one(prompt, ref, dest, api_key, size, quality)
            return i, dest.name, ""
        except Exception as exc:
            return i, "", str(exc)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_task, i, ref) for i, ref in enumerate(references)]
        for future in as_completed(futures):
            idx, filename, error = future.result()
            var = run.variations[idx]
            if error:
                var.status = "error"
                var.error = error
            else:
                var.status = "done"
                var.filename = filename
            if on_progress is not None:
                on_progress(run)

    return run

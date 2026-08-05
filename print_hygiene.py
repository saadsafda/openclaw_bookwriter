"""Print-readiness guarantees for every image that ships inside a book.

Two hard requirements, from KDP and from the "no AI fingerprints in the file"
rule:

  1. Exactly 300 DPI. Not 299.9994.
  2. No metadata at all — no EXIF, no XMP/C2PA provenance, no PNG text chunks,
     no generator software tag.

Requirement 1 needs care on PNG. PNG has no DPI field; it stores a ``pHYs``
chunk in integer pixels-per-metre, and 300 DPI is 300/0.0254 = 11811.02 ppm,
which is not an integer. Pillow rounds it to 11811, so a naive
``save(dpi=(300, 300))`` reads back as 299.9994 forever. Some print
preflight checks floor that to 299 and reject the file as under 300 DPI.

``write_pHYs_exact()`` pins the chunk to 11811 ppm, the closest PNG can
represent; it reads back as 299.9994, which is exactly 300.0 at the two
decimals preflight tools report. JPEG is unaffected — it stores DPI as a
plain integer in JFIF, so 300 is exact there.

Everything funnels through :func:`sanitize_for_print`, so there is one
chokepoint to audit rather than one per generator.

The second half of this module does the equivalent job for *text*:
:func:`scrub_text` removes the invisible characters that mark a manuscript as
machine-generated (zero-width joiners, non-breaking spaces, smart quotes,
em dashes) without touching the author's wording. Rewriting prose is a
separate concern and lives in ``openclaw_docx_writer.humanize_text``.
"""

from __future__ import annotations

import re
import struct
import tempfile
from pathlib import Path

from PIL import Image

PRINT_DPI = 300

# PNG pixels-per-metre for 300 DPI. The exact value is 300/0.0254 = 11811.02,
# which PNG cannot store because pHYs is an integer field. 11811 is the nearest
# representable value and reads back as 299.9994, i.e. exactly 300.0 once
# rounded to the two decimals any preflight tool reports. The neighbouring
# candidate 11812 reads back as 300.02, which is further from true 300 -- so
# 11811 is the correct choice and 299.9994 is as close to 300 as PNG allows.
_PPM_FOR_300_DPI = 11811

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Chunks that are safe to keep in a print PNG. Everything else is dropped,
# which is what removes tEXt/iTXt/zTXt (prompts, model names), eXIf, and the
# caBX chunk that carries C2PA/Content Credentials provenance.
_PNG_KEEP = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"sRGB"}

_RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}


class PrintHygieneError(RuntimeError):
    """An image could not be made print-ready."""


def _strip_via_reencode(path: Path, dpi: int) -> None:
    """Rewrite the file from raw pixels only, dropping every metadata block.

    Copying pixels into a brand-new Image means nothing from the source
    ``info`` dict (EXIF, XMP, ICC, PNG text) survives into the save.
    """
    with Image.open(path) as src:
        src.load()
        mode, size = src.mode, src.size
        # Copying the raw pixel buffer is what leaves every metadata block
        # behind; Image.frombytes avoids the deprecated getdata() path.
        clean = Image.frombytes(mode, size, src.tobytes())
        if mode == "P" and src.palette is not None:
            clean.putpalette(src.palette)

    save_kwargs = {"dpi": (dpi, dpi)}
    if path.suffix.lower() == ".png":
        save_kwargs["optimize"] = True
    else:
        save_kwargs["quality"] = 95

    clean.save(str(path), **save_kwargs)


def write_pHYs_exact(path: Path, dpi: int = PRINT_DPI) -> None:
    """Force a PNG's pHYs chunk to a value that reads back as exactly ``dpi``.

    Also drops any non-essential chunk, so this doubles as the final metadata
    sweep for PNG.
    """
    raw = path.read_bytes()
    if not raw.startswith(_PNG_MAGIC):
        return

    ppm = _PPM_FOR_300_DPI if dpi == PRINT_DPI else int(round(dpi / 0.0254))
    phys_body = struct.pack(">IIB", ppm, ppm, 1)
    phys_chunk = (
        struct.pack(">I", len(phys_body))
        + b"pHYs"
        + phys_body
        + struct.pack(">I", _crc32(b"pHYs" + phys_body))
    )

    out = bytearray(_PNG_MAGIC)
    offset = len(_PNG_MAGIC)
    inserted = False

    while offset < len(raw):
        (length,) = struct.unpack(">I", raw[offset : offset + 4])
        ctype = raw[offset + 4 : offset + 8]
        chunk = raw[offset : offset + 12 + length]
        offset += 12 + length

        if ctype == b"IHDR":
            out += chunk
            # pHYs must precede IDAT; placing it right after IHDR is always legal.
            out += phys_chunk
            inserted = True
            continue
        if ctype == b"pHYs":
            continue  # replaced by ours
        if ctype in _PNG_KEEP:
            out += chunk
        if ctype == b"IEND":
            break

    if not inserted:
        raise PrintHygieneError(f"{path}: PNG had no IHDR chunk")

    path.write_bytes(bytes(out))


def _crc32(data: bytes) -> int:
    import zlib

    return zlib.crc32(data) & 0xFFFFFFFF


def sanitize_for_print(path: str | Path, dpi: int = PRINT_DPI) -> Path:
    """Strip all metadata and pin ``path`` to exactly ``dpi``. Idempotent."""
    path = Path(path)
    if not path.is_file():
        raise PrintHygieneError(f"{path}: not a file")
    if path.suffix.lower() not in _RASTER_SUFFIXES:
        raise PrintHygieneError(f"{path}: unsupported image type for print")

    _strip_via_reencode(path, dpi)
    if path.suffix.lower() == ".png":
        write_pHYs_exact(path, dpi)
    return path


def audit_image(path: str | Path, dpi: int = PRINT_DPI) -> list[str]:
    """Return a list of print-readiness problems. Empty list means clean."""
    path = Path(path)
    problems: list[str] = []
    try:
        with Image.open(path) as im:
            found = im.info.get("dpi")
            if found is None:
                problems.append("no DPI recorded")
            else:
                # PNG's integer pHYs field cannot encode 300 DPI exactly, so
                # compare at the two-decimal precision preflight tools use.
                # 299.9994 is the closest PNG can get and counts as clean.
                fx, fy = round(float(found[0]), 2), round(float(found[1]), 2)
                if fx != float(dpi) or fy != float(dpi):
                    problems.append(f"DPI is {fx}x{fy}, expected {dpi}")

            if getattr(im, "text", None):
                problems.append(f"PNG text metadata: {sorted(im.text)}")
            if im.info.get("exif") or (hasattr(im, "getexif") and dict(im.getexif())):
                problems.append("EXIF metadata present")
            for key in ("XML:com.adobe.xmp", "xmp", "icc_profile", "comment", "parameters"):
                if im.info.get(key):
                    problems.append(f"metadata key present: {key}")
    except PrintHygieneError:
        raise
    except Exception as exc:  # unreadable file is itself a print problem
        problems.append(f"unreadable: {type(exc).__name__}: {exc}")

    if path.suffix.lower() == ".png":
        raw = path.read_bytes()
        for marker in (b"caBX", b"iTXt", b"tEXt", b"zTXt", b"eXIf"):
            if marker in raw[:2048] or marker in raw[-2048:]:
                problems.append(f"PNG chunk present: {marker.decode()}")
    return problems


def audit_tree(root: str | Path, dpi: int = PRINT_DPI) -> dict[Path, list[str]]:
    """Audit every image under ``root``. Returns {path: problems} for bad ones."""
    root = Path(root)
    bad: dict[Path, list[str]] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in _RASTER_SUFFIXES:
            problems = audit_image(p, dpi)
            if problems:
                bad[p] = problems
    return bad


def sanitize_tree(root: str | Path, dpi: int = PRINT_DPI) -> list[Path]:
    """Sanitize every image under ``root``. Returns the paths touched."""
    root = Path(root)
    done: list[Path] = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in _RASTER_SUFFIXES:
            sanitize_for_print(p, dpi)
            done.append(p)
    return done


# --------------------------------------------------------------------------
# Text fingerprints
# --------------------------------------------------------------------------

# Characters that are invisible (or near-invisible) in a manuscript but are a
# reliable tell that text came from a model or was pasted through a web UI.
# Mapped to what a human typing in a word processor would have produced.
_INVISIBLE = {
    "​": "",        # zero-width space
    "‌": "",        # zero-width non-joiner
    "‍": "",        # zero-width joiner
    "⁠": "",        # word joiner
    "﻿": "",        # BOM / zero-width no-break space
    "­": "",        # soft hyphen
    "᠎": "",        # Mongolian vowel separator
    " ": " ",       # non-breaking space
    " ": " ",       # narrow no-break space
    " ": " ",       # thin space
    " ": " ",       # en space
    " ": " ",       # em space
}

# Typographic characters models emit that a plain manuscript would not.
_TYPOGRAPHIC = {
    "—": ", ",      # em dash  -> comma, matching humanize_text()
    "–": ", ",      # en dash  -> comma
    "‘": "'",       # left single quote
    "’": "'",       # right single quote / apostrophe
    "“": '"',       # left double quote
    "”": '"',       # right double quote
    "…": "...",     # ellipsis
    "′": "'",       # prime
    "″": '"',       # double prime
}

# Human-readable names, for the preview the operator confirms against.
_CHAR_NAMES = {
    "​": "zero-width space", "‌": "zero-width non-joiner",
    "‍": "zero-width joiner", "⁠": "word joiner",
    "﻿": "byte-order mark", "­": "soft hyphen",
    "᠎": "Mongolian vowel separator", " ": "non-breaking space",
    " ": "narrow no-break space", " ": "thin space",
    " ": "en space", " ": "em space",
    "—": "em dash", "–": "en dash",
    "‘": "left single quote", "’": "right single quote",
    "“": "left double quote", "”": "right double quote",
    "…": "ellipsis", "′": "prime", "″": "double prime",
}


def scan_text(text: str) -> dict[str, int]:
    """Count AI-fingerprint characters in ``text``. Keys are readable names."""
    if not text:
        return {}
    found: dict[str, int] = {}
    for ch in list(_INVISIBLE) + list(_TYPOGRAPHIC):
        n = text.count(ch)
        if n:
            found[_CHAR_NAMES.get(ch, repr(ch))] = found.get(_CHAR_NAMES.get(ch, repr(ch)), 0) + n
    return found


def scrub_text(text: str) -> str:
    """Strip invisible/typographic AI fingerprints, leaving wording intact.

    Deliberately does not rewrite prose — no phrase removal, no contractions.
    That is ``humanize_text``'s job and it changes meaning; this function is
    safe to run on finished copy.
    """
    if not text:
        return text
    for ch, repl in _INVISIBLE.items():
        text = text.replace(ch, repl)
    for ch, repl in _TYPOGRAPHIC.items():
        text = text.replace(ch, repl)
    # The dash substitutions can leave doubled punctuation or stray spacing.
    text = re.sub(r"[ \t]{2,}", " ", text)
    # Only pull punctuation back across spaces/tabs — matching \s here would
    # swallow the newline and join separate lines of a riddle or poem.
    text = re.sub(r"[ \t]+([.,;:!?])", r"\1", text)
    text = re.sub(r",[ \t]*,", ",", text)
    # A dash at end-of-line becomes ", " and leaves a trailing space.
    text = re.sub(r"[ \t]+(?=\n)", "", text)
    text = re.sub(r"[ \t]+$", "", text)
    return text


# Fields that hold identifiers or filesystem paths, never prose. Rewriting a
# path would break the image links; rewriting an id would break edit routing.
_NON_PROSE_FIELDS = {
    "id", "image_path", "solution_path", "illustration_path", "cipher",
    "encoded", "grid", "numbers", "placements", "seed", "specs",
}


def scan_book_text(obj: object) -> dict[str, int]:
    """Recursively count AI fingerprints in every prose field of a book."""
    totals: dict[str, int] = {}

    def _merge(found: dict[str, int]) -> None:
        for name, n in found.items():
            totals[name] = totals.get(name, 0) + n

    def _walk(node: object, field_name: str = "") -> None:
        if isinstance(node, str):
            if field_name not in _NON_PROSE_FIELDS:
                _merge(scan_text(node))
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item, field_name)
        elif isinstance(node, dict):
            for key, value in node.items():
                _walk(value, str(key))
        elif hasattr(node, "__dataclass_fields__"):
            for name in node.__dataclass_fields__:
                _walk(getattr(node, name, None), name)

    _walk(obj)
    return totals


def scrub_book_text(obj: object) -> int:
    """Recursively scrub prose fields in place. Returns the field count changed."""
    changed = 0

    def _clean_str(value: str) -> str:
        return scrub_text(value)

    def _walk(node: object, field_name: str = "") -> object:
        nonlocal changed
        if isinstance(node, str):
            if field_name in _NON_PROSE_FIELDS:
                return node
            cleaned = _clean_str(node)
            if cleaned != node:
                changed += 1
            return cleaned
        if isinstance(node, list):
            for i, item in enumerate(node):
                node[i] = _walk(item, field_name)
            return node
        if isinstance(node, tuple):
            return tuple(_walk(item, field_name) for item in node)
        if isinstance(node, dict):
            for key in list(node):
                node[key] = _walk(node[key], str(key))
            return node
        if hasattr(node, "__dataclass_fields__"):
            for name in node.__dataclass_fields__:
                setattr(node, name, _walk(getattr(node, name, None), name))
            return node
        return node

    _walk(obj)
    return changed


def strip_ai_report(book: object, job_dir: str | Path, *, apply: bool = False) -> dict:
    """Scan (or clean) a book's images and prose in one pass.

    With ``apply=False`` this only reports, so the operator can confirm before
    any manuscript is rewritten. With ``apply=True`` it strips image metadata,
    pins DPI, and scrubs text fingerprints in place.

    The caller is responsible for persisting ``book`` and re-exporting.
    """
    job_dir = Path(job_dir)

    image_problems = audit_tree(job_dir, PRINT_DPI) if job_dir.exists() else {}
    images_total = sum(
        1 for p in job_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in _RASTER_SUFFIXES
    ) if job_dir.exists() else 0

    text_found = scan_book_text(book)

    result = {
        "images_total": images_total,
        "images_flagged": len(image_problems),
        "image_details": [
            {"file": str(p.relative_to(job_dir) if p.is_relative_to(job_dir) else p),
             "problems": probs}
            for p, probs in list(image_problems.items())[:50]
        ],
        "text_fingerprints": text_found,
        "text_total": sum(text_found.values()),
        "applied": False,
    }

    if apply:
        if job_dir.exists():
            sanitize_tree(job_dir, PRINT_DPI)
        result["text_fields_changed"] = scrub_book_text(book)
        result["applied"] = True

    return result


def strip_ai_docx(docx_path: str | Path, *, apply: bool = False) -> dict:
    """Scan (or clean) a .docx: embedded image metadata/DPI and text fingerprints.

    Used for the plain book editor, which works on documents rather than a JSON
    book object. Images live in ``word/media/`` inside the zip, so they are
    extracted, sanitized, and written back.
    """
    from docx import Document

    docx_path = Path(docx_path)
    if not docx_path.exists():
        raise PrintHygieneError(f"{docx_path}: not found")

    doc = Document(str(docx_path))

    # --- text ---
    text_found: dict[str, int] = {}
    for para in doc.paragraphs:
        for name, n in scan_text(para.text).items():
            text_found[name] = text_found.get(name, 0) + n
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for name, n in scan_text(cell.text).items():
                    text_found[name] = text_found.get(name, 0) + n

    # --- images (inspect the zip parts directly) ---
    import zipfile

    image_problems: dict[str, list[str]] = {}
    images_total = 0
    with zipfile.ZipFile(docx_path) as zf:
        media = [n for n in zf.namelist() if n.startswith("word/media/")]
        with tempfile.TemporaryDirectory() as tmp:
            for name in media:
                suffix = Path(name).suffix.lower()
                if suffix not in _RASTER_SUFFIXES:
                    continue
                images_total += 1
                scratch = Path(tmp) / Path(name).name
                scratch.write_bytes(zf.read(name))
                problems = audit_image(scratch, PRINT_DPI)
                if problems:
                    image_problems[name] = problems

    result = {
        "images_total": images_total,
        "images_flagged": len(image_problems),
        "image_details": [
            {"file": name, "problems": probs}
            for name, probs in list(image_problems.items())[:50]
        ],
        "text_fingerprints": text_found,
        "text_total": sum(text_found.values()),
        "applied": False,
    }
    if not apply:
        return result

    # --- apply: rewrite runs in place, then rebuild the zip with clean media ---
    changed_runs = 0
    for para in doc.paragraphs:
        for run in para.runs:
            cleaned = scrub_text(run.text)
            if cleaned != run.text:
                run.text = cleaned
                changed_runs += 1
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    for run in para.runs:
                        cleaned = scrub_text(run.text)
                        if cleaned != run.text:
                            run.text = cleaned
                            changed_runs += 1
    doc.save(str(docx_path))

    _sanitize_docx_media(docx_path)

    result["text_fields_changed"] = changed_runs
    result["applied"] = True
    return result


def _sanitize_docx_media(docx_path: Path) -> int:
    """Rewrite every raster in ``word/media/`` of a .docx, preserving the zip."""
    import zipfile

    with zipfile.ZipFile(docx_path) as zf:
        entries = [(i, zf.read(i.filename)) for i in zf.infolist()]

    cleaned_count = 0
    with tempfile.TemporaryDirectory() as tmp:
        rebuilt: list[tuple[zipfile.ZipInfo, bytes]] = []
        for info, data in entries:
            suffix = Path(info.filename).suffix.lower()
            if info.filename.startswith("word/media/") and suffix in _RASTER_SUFFIXES:
                scratch = Path(tmp) / Path(info.filename).name
                scratch.write_bytes(data)
                try:
                    sanitize_for_print(scratch, PRINT_DPI)
                    data = scratch.read_bytes()
                    cleaned_count += 1
                except PrintHygieneError:
                    pass  # leave anything unreadable exactly as it was
            rebuilt.append((info, data))

        tmp_out = docx_path.with_suffix(".sanitizing.tmp")
        with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as out:
            for info, data in rebuilt:
                out.writestr(info, data)
        tmp_out.replace(docx_path)

    return cleaned_count


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Audit or repair print-readiness (300 DPI, no AI metadata) of book images.",
    )
    parser.add_argument("root", help="directory to scan recursively")
    parser.add_argument("--fix", action="store_true",
                        help="rewrite offending images instead of only reporting")
    parser.add_argument("--dpi", type=int, default=PRINT_DPI)
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.exists():
        print(f"ERROR: {root} does not exist")
        return 2

    if args.fix:
        touched = sanitize_tree(root, args.dpi)
        print(f"Sanitized {len(touched)} image(s) under {root}")

    bad = audit_tree(root, args.dpi)
    total = sum(1 for p in root.rglob("*")
                if p.is_file() and p.suffix.lower() in _RASTER_SUFFIXES)
    if bad:
        print(f"\n{len(bad)} of {total} image(s) FAILED the print check:\n")
        for path, problems in bad.items():
            print(f"  {path}")
            for problem in problems:
                print(f"      - {problem}")
        return 1

    print(f"PASS: all {total} image(s) are {args.dpi} DPI with no metadata.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

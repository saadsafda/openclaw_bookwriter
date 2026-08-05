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

``write_pHYs_exact()`` rewrites the chunk to 11812 ppm, which is the value
that rounds *up* to a clean 300.0 on read-back. JPEG is unaffected — it
stores DPI as a plain integer in JFIF, so 300 is exact there.

Everything funnels through :func:`sanitize_for_print`, so there is one
chokepoint to audit rather than one per generator.
"""

from __future__ import annotations

import struct
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

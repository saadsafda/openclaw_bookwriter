#!/usr/bin/env python3
"""Build one complete puzzle book from the command line, for testing.

This is the standalone counterpart to the /puzzle web page: same engine, same
pipeline, same exporters, no Flask and no database. Use it to eyeball a real
book end to end before running a full-size job through the UI.

Defaults to a deliberately SMALL book so a test run is cheap and quick. Pass
--full for the real pilot-book counts from the spec (12 mazes, 20 riddles, 10
word searches, 10 cryptograms, 6 trivia chapters, 14 crosswords).

Examples
--------
    # Smallest useful book: a few of everything, ~10 model calls.
    python make_test_puzzle_book.py

    # No model calls at all — grids and rendering only. Free, ~10 seconds.
    python make_test_puzzle_book.py --offline

    # A different subject.
    python make_test_puzzle_book.py \
        --title "Space Mission Puzzle Book" --topic "space exploration"

    # The real pilot book at full size.
    python make_test_puzzle_book.py --full

Output goes to puzzle_test_output/<slug>/ and includes the DOCX, the KDP
print files, every puzzle and solution PNG, and the formatter handoff zip.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from puzzle import engine, export, pipeline
from puzzle.engine import (
    ALL_SECTIONS,
    SECTION_CROSSWORDS,
    SECTION_CRYPTOGRAMS,
    SECTION_LABELS,
    SECTION_MAZES,
    SECTION_PICTURE,
    SECTION_RIDDLES,
    SECTION_TRIVIA,
    SECTION_WORDSEARCH,
    BookConfig,
)

ROOT = Path(__file__).resolve().parent
OUTPUT_ROOT = ROOT / "puzzle_test_output"

# A small book: enough of each section to prove the whole pipeline works,
# without paying for a hundred puzzles.
SMALL_COUNTS = {
    SECTION_PICTURE: 2,
    SECTION_MAZES: 3,
    SECTION_RIDDLES: 4,
    SECTION_WORDSEARCH: 2,
    SECTION_CRYPTOGRAMS: 3,
    SECTION_TRIVIA: 1,      # 1 chapter = 10 questions
    SECTION_CROSSWORDS: 2,
}

# The pilot book from the spec.
PILOT_TITLE = "Crime Scene Puzzle Book for Kids Ages 8-12"
PILOT_TOPIC = "crime scene investigation and detective work"
PILOT_AUDIENCE = "kids ages 8-12"

# Section 4 of the spec lists these ten word-search topics for the pilot book.
PILOT_WORDSEARCH_SUBJECTS = [
    "Detective Tools", "Types of Clues", "Police Station", "Mystery Words",
    "Spy Gear", "City Locations", "Vehicles", "Forensics Basics",
    "Courtroom Terms", "Secret Agents",
]


# --------------------------------------------------------------------------
# Offline mode: canned replies so the grids and exports can be tested for free
# --------------------------------------------------------------------------

_OFFLINE_WORDS = [
    "DETECTIVE", "CLUE", "SUSPECT", "MOTIVE", "ALIBI", "WITNESS", "EVIDENCE",
    "BADGE", "SIREN", "CAMERA", "GLOVES", "MARKER", "SAMPLE", "REPORT",
]
_OFFLINE_CROSSWORD = ["DETECTIVE", "CLUE", "SUSPECT", "MOTIVE", "ALIBI", "WITNESS"]

_offline_counter = {"n": 0}


def _offline_call(agent_id: str, message: str, **kwargs) -> str:
    """Stand-in for engine.call_openclaw_raw that never hits the network."""
    import re

    _offline_counter["n"] += 1
    uniq = _offline_counter["n"] * 100
    m = message

    def _count(pattern: str, default: int = 5) -> int:
        found = re.search(pattern, m)
        return int(found.group(1)) if found else default

    if "spot-the-difference" in m:
        n = _count(r"Generate (\d+) spot")
        return json.dumps([{
            "scene_title": f"Sample Scene {i + 1}",
            "scene_description": (
                "A cluttered detective's office: a desk with a lamp and an open "
                "case file, a coat on a hook, a plant by the window, and a "
                "corkboard covered in photos and pinned notes."
            ),
            "difference_ideas": [
                "the lamp shade is a different shape",
                "one photo is missing from the corkboard",
                "the plant has an extra leaf",
                "the coat hook is empty",
                "the desk drawer is open instead of shut",
                "a pencil is added beside the file",
                "the window blind is higher",
                "the clock hands point elsewhere",
            ],
        } for i in range(n)])

    if "riddles for this book" in m:
        n = _count(r"Write (\d+) riddles")
        return json.dumps([{
            "riddle": (
                f"I get left behind at the scene, and I can point straight to "
                f"the one who was there.\nWhat am I, number {uniq + i}?"
            ),
            "answer": f"Sample Answer {uniq + i}",
        } for i in range(n)])

    if "word search puzzle on this topic" in m:
        return json.dumps(_OFFLINE_WORDS[:9])

    if "cryptogram puzzles" in m:
        n = _count(r"Write (\d+) short phrases")
        return json.dumps([{
            "phrase": f"A good detective always checks every single clue {uniq + i}",
            "hint": "Think about what an investigator does first",
        } for i in range(n)])

    if "chapter themes" in m:
        n = _count(r"Propose (\d+) chapter themes")
        return json.dumps([{
            "chapter_title": f"Sample Chapter Theme {i + 1}",
            "chapter_scope": "A slice of the book topic.",
        } for i in range(n)])

    if "multiple-choice trivia" in m:
        n = _count(r"Write (\d+) multiple-choice")
        return json.dumps([{
            "question": f"Which tool would a detective reach for, number {uniq + i}?",
            "choices": {
                "A": f"Option A{uniq + i}", "B": f"Option B{uniq + i}",
                "C": f"Option C{uniq + i}", "D": f"Option D{uniq + i}",
            },
            "correct_answer": "A",
        } for i in range(n)])

    if "crossword puzzle on this topic" in m:
        # Clues must not contain the word "clue" — CLUE is one of the answers,
        # and a clue naming its own answer is correctly rejected.
        return json.dumps([
            {"word": w, "clue": f"Sample hint number {i + 1} for this entry"}
            for i, w in enumerate(_OFFLINE_CROSSWORD)
        ])

    if "subjects for the" in m:
        n = _count(r"Propose (\d+) subjects")
        return json.dumps([f"Sample Subject {i + 1}" for i in range(n)])

    raise engine.PuzzleError("Offline mode got an unexpected prompt.")


def enable_offline_mode() -> None:
    engine.call_openclaw_raw = _offline_call
    pipeline.engine.call_openclaw_raw = _offline_call


# --------------------------------------------------------------------------

def slugify(text: str) -> str:
    keep = [c if c.isalnum() or c in " -_" else "" for c in (text or "book")]
    return ("".join(keep).strip().replace(" ", "_") or "puzzle_book")[:60].lower()


def build_config(args: argparse.Namespace) -> BookConfig:
    counts = dict(engine.DEFAULT_COUNTS) if args.full else dict(SMALL_COUNTS)

    # Per-section overrides, so a run can target just one section.
    for kind in ALL_SECTIONS:
        override = getattr(args, kind, None)
        if override is not None:
            counts[kind] = override

    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        unknown = wanted - set(ALL_SECTIONS)
        if unknown:
            raise SystemExit(
                f"Unknown section(s): {', '.join(sorted(unknown))}\n"
                f"Valid names: {', '.join(ALL_SECTIONS)}"
            )
        counts = {k: (v if k in wanted else 0) for k, v in counts.items()}

    sections: dict[str, dict] = {}
    for kind in ALL_SECTIONS:
        section: dict = {"count": counts.get(kind, 0), "enabled": counts.get(kind, 0) > 0}
        # Use the spec's own word-search topics when building the pilot book.
        if kind == SECTION_WORDSEARCH and args.topic == PILOT_TOPIC:
            section["subjects"] = PILOT_WORDSEARCH_SUBJECTS[:counts.get(kind, 0)]
        sections[kind] = section

    return BookConfig.from_dict({
        "book_title": args.title,
        "topic": args.topic,
        "audience": args.audience,
        "difficulty": args.difficulty,
        "agent": args.agent,
        "thinking": args.thinking,
        "wordsearch_grid": args.grid,
        "maze_cols": args.maze_cols,
        "maze_rows": args.maze_rows,
        "sections": sections,
    })


def main() -> int:
    p = argparse.ArgumentParser(
        description="Build one puzzle book end to end, for testing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples\n--------\n")[-1],
    )
    p.add_argument("--title", default=PILOT_TITLE, help="Book title.")
    p.add_argument("--topic", default=PILOT_TOPIC, help="What the book is about.")
    p.add_argument("--audience", default=PILOT_AUDIENCE, help="Who it is for.")
    p.add_argument("--difficulty", default="medium",
                   choices=["easy", "medium", "hard"],
                   help="Also controls word-search directions. Default: medium.")

    p.add_argument("--full", action="store_true",
                   help="Use the spec's full pilot-book counts instead of a small test book.")
    p.add_argument("--only", default="",
                   help="Comma-separated section names to build, e.g. --only mazes,crosswords")
    p.add_argument("--offline", action="store_true",
                   help="Use canned text instead of calling the model. Free; tests grids and exports.")

    p.add_argument("--agent", default="main", help="OpenClaw agent id. Default: main.")
    p.add_argument("--thinking", default="", choices=["", "low", "medium", "high"],
                   help="OpenClaw thinking level.")
    p.add_argument("--grid", type=int, default=13, help="Word search grid size. Default: 13.")
    p.add_argument("--maze-cols", type=int, default=14, dest="maze_cols")
    p.add_argument("--maze-rows", type=int, default=20, dest="maze_rows")
    p.add_argument("--seed", type=int, default=20260731,
                   help="Layout seed. Same seed rebuilds identical puzzles.")

    p.add_argument("--out", default="", help="Output directory. Default: puzzle_test_output/<slug>")
    p.add_argument("--keep", action="store_true",
                   help="Keep any existing output instead of clearing it first.")
    p.add_argument("--no-kdp", action="store_true", help="Skip the KDP 6x9 print files.")

    for kind in ALL_SECTIONS:
        p.add_argument(f"--{kind.replace('_', '-')}", type=int, default=None, dest=kind,
                       metavar="N",
                       help=f"How many {SECTION_LABELS[kind].lower()} to build.")

    args = p.parse_args()

    if args.offline:
        enable_offline_mode()

    try:
        cfg = build_config(args)
    except engine.PuzzleError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    out_dir = Path(args.out) if args.out else OUTPUT_ROOT / slugify(args.title)
    if out_dir.exists() and not args.keep:
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    est = cfg.page_estimate()
    planned = [
        f"{cfg.section(k).count} {SECTION_LABELS[k].lower()}"
        for k in ALL_SECTIONS if cfg.section(k).enabled and cfg.section(k).count
    ]

    print("=" * 68)
    print(f"  {cfg.book_title}")
    print("=" * 68)
    print(f"  Topic     : {cfg.topic}")
    print(f"  Audience  : {cfg.audience}")
    print(f"  Building  : {', '.join(planned) or 'nothing — every section is empty'}")
    print(f"  Estimate  : ~{est['total_pages']} pages "
          f"({est['puzzle_pages']} puzzle + {est['answer_key_pages']} answer key "
          f"+ {est['title_pages']} front matter)")
    print(f"  Mode      : {'OFFLINE (canned text, no model calls)' if args.offline else f'live, agent={cfg.agent}'}")
    print(f"  Output    : {out_dir}")
    print("=" * 68)
    print()

    if not planned:
        print("Nothing to build. Check --only / --<section> values.", file=sys.stderr)
        return 2

    started = time.time()

    def log(message: str) -> None:
        print(f"  {message}", flush=True)

    def progress(stage: str, pct: float) -> None:
        print(f"\n[{pct * 100:5.1f}%] {stage}", flush=True)

    try:
        builder = pipeline.PuzzleBuilder(
            cfg,
            log=log,
            progress=progress,
            cache_dir=out_dir / "cache",
            seed=args.seed,
        )
        book = builder.build(out_dir)
    except engine.PuzzleError as exc:
        print(f"\nBuild failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130

    # -- exports -----------------------------------------------------------
    print("\n[ 90.0%] exporting")
    json_path = pipeline.write_json(book, out_dir / "puzzle_book.json")
    md_path = export.write_markdown(book, out_dir / "puzzle_book.md")
    docx_path = export.build_docx(book, out_dir / "puzzle_book.docx")
    log(f"wrote {docx_path.name}, {md_path.name}, {json_path.name}")

    image_problems = export.verify_print_images(book, out_dir)
    if image_problems:
        for problem in image_problems:
            log(f"WARNING: print check — {problem}")
    else:
        log("print check passed: all images 300 DPI, metadata clean")

    if not args.no_kdp:
        try:
            kdp = export.build_kdp_files(book, docx_path, out_dir)
            log(f"KDP print files ready (~{kdp['estimated_pages']} pages, "
                f"inside margin {kdp['inside_margin_in']} in)")
        except Exception as exc:  # noqa: BLE001 - never lose the manuscript
            log(f"WARNING: KDP formatting failed: {exc}")
            book.warnings.append(f"KDP formatting failed: {exc}")

    zip_path = export.build_handoff_zip(
        book, out_dir, out_dir / f"{slugify(cfg.book_title)}_handoff.zip")
    log(f"packed {zip_path.name}")

    # -- summary -----------------------------------------------------------
    elapsed = time.time() - started
    counts = book.counts()
    usage = book.usage or {}

    print()
    print("=" * 68)
    print("  BUILD COMPLETE")
    print("=" * 68)
    for kind in ALL_SECTIONS:
        want = cfg.section(kind).count
        if not cfg.section(kind).enabled or not want:
            continue
        got = counts.get(kind, 0)
        mark = "ok " if got >= want else "!! "
        unit = "chapters" if kind == SECTION_TRIVIA else "puzzles"
        print(f"  {mark}{SECTION_LABELS[kind]:<20} {got}/{want} {unit}")

    images = sum(
        1 for group in (book.mazes, book.word_searches, book.crosswords)
        for item in group for path in (item.image_path, item.solution_path)
        if path and Path(path).exists()
    )
    print(f"\n  Rendered images : {images} PNG at 300 DPI, grayscale, 6x9 in")
    print(f"  Elapsed         : {elapsed:.1f}s")
    if usage.get("calls"):
        print(f"  Model calls     : {usage['calls']}"
              + (f" (+{usage['cache_hits']} from cache)" if usage.get("cache_hits") else ""))
        print(f"  Tokens          : {usage.get('total_tokens', 0):,}")
        print(f"  Cost            : ${usage.get('cost_usd', 0):.4f}")

    if book.warnings:
        print(f"\n  Warnings ({len(book.warnings)}):")
        for w in book.warnings:
            print(f"    - {w}")

    print("\n  Files:")
    for label, path in (
        ("Handoff ZIP", zip_path),
        ("Manuscript ", docx_path),
        ("Markdown   ", md_path),
        ("JSON       ", json_path),
    ):
        if path.exists():
            print(f"    {label} {path}  ({path.stat().st_size / 1024:.0f} KB)")
    for extra in sorted(out_dir.glob("*_kindle.docx")) + sorted(out_dir.glob("*_paperback.docx")):
        print(f"    KDP         {extra}  ({extra.stat().st_size / 1024:.0f} KB)")

    print(f"\n  Puzzle art  : {out_dir}/mazes, /word_searches, /crosswords")
    print("=" * 68)

    # A section that came up short is a soft failure worth a non-zero exit.
    short = [
        k for k in ALL_SECTIONS
        if cfg.section(k).enabled and cfg.section(k).count
        and counts.get(k, 0) < cfg.section(k).count
    ]
    return 3 if short else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Cross-section formulaic-writing checks must fire on molds and stay quiet
on varied prose.

The detectors in openclaw_docx_writer.py all run inside
_generate_clean_paragraph_once, against a single freshly generated paragraph.
A paragraph that is fine on its own always passes, so a book where every
section's third paragraph makes the identical move ships clean -- the pattern
only exists across sections, after assembly. These tests pin that gap closed.

The false-negative half matters as much as the false-positive half: an audit
that flags ordinary prose gets ignored, which is the same as not having one.
"""

from __future__ import annotations

import pytest
from docx import Document

from manuscript_audit import (
    audit,
    collect_proper_nouns,
    opener_shape,
    read_sections,
)


def _kinds(report, kind):
    return [f for f in report["findings"] if f["kind"] == kind]


def _build(tmp_path, sections, name="book.docx"):
    """Write a .docx of (heading, [paragraphs]) pairs."""
    doc = Document()
    for title, paras in sections:
        doc.add_heading(title, level=1)
        for para in paras:
            doc.add_paragraph(para)
    path = tmp_path / name
    doc.save(str(path))
    return path


# Four distinct openings, so nothing repeats across sections by accident.
VARIED = [
    "Dust hung over the valley long after the wagons had gone, and the light "
    "came through it the color of weak tea. Farmers said you could taste the "
    "summer in the air by August, and most of them meant it as a complaint.",
    "Rain arrived in September without much ceremony. The creek rose over the "
    "ford and stayed there for a week, which meant the mail came late and the "
    "schoolhouse stood empty while the children worked at home instead.",
    "Iron rails reached the county in a single season, and the sound of them "
    "changed how people spoke about distance. A trip that had cost three days "
    "now cost an afternoon, and the older men resented it more than they said.",
    "Snow closed the pass in November and did not open it again until March. "
    "Supplies came in by sled when they came at all, and the store kept a "
    "ledger of debts that everyone understood would be settled in the spring.",
]

MOLD_PARA = (
    "{name} checked the fuel line before dawn and found it clogged again with "
    "grit from the previous day. He cleared it with a length of wire and washed "
    "his hands in the creek, and the engine was turning over cleanly by sunup."
)


def test_positional_mold_detected(tmp_path):
    """Every section's 3rd paragraph opening on a named character acting."""
    names = ["Jackson", "Crocker", "Sewall", "Harris", "Miller", "Bennett"]
    sections = [
        (f"Chapter {i + 1}",
         [VARIED[0], VARIED[1], MOLD_PARA.format(name=n), VARIED[3]])
        for i, n in enumerate(names)
    ]
    report = audit(_build(tmp_path, sections))
    molds = _kinds(report, "positional-mold")
    third = [m for m in molds if m["detail"].startswith("paragraph 3 ")]
    assert third, f"3rd-paragraph mold not flagged; got {[m['detail'] for m in molds]}"
    assert "proper-noun + verb opener" in third[0]["detail"]
    assert third[0]["count"] == len(names)


def test_varied_prose_has_no_positional_mold(tmp_path):
    """Rotating the paragraph order per section must not trip the mold check."""
    sections = [
        (f"Chapter {i + 1}", VARIED[i % 4:] + VARIED[:i % 4])
        for i in range(6)
    ]
    report = audit(_build(tmp_path, sections))
    assert not _kinds(report, "positional-mold")


def test_repeated_closer_detected(tmp_path):
    """The same summarising last line in every section is a template."""
    closer = (
        "The families who stayed were the ones who owned their land outright "
        "and could afford to wait for whatever arrived next. It was enough."
    )
    sections = [(f"Chapter {i + 1}", [VARIED[i % 4], VARIED[(i + 1) % 4], closer])
                for i in range(6)]
    report = audit(_build(tmp_path, sections))
    assert _kinds(report, "repeated-closer")


def test_pet_phrase_merges_sliding_windows(tmp_path):
    """One repeated sentence reports as a single phrase, not several fragments.

    Overlapping n-gram windows over the same sentence would otherwise emit a
    chain of near-identical rows and bury the finding.
    """
    tic = "At the end of the day it comes down to patience."
    # Every surrounding paragraph must be unique, or the repeated VARIED text
    # would itself be the longest repeat and mask the tic under test.
    sections = [
        (f"Chapter {i + 1}",
         [f"{tic} The {noun} changed hands twice that year and nobody kept a "
          f"record of the second sale, which is why the dispute lasted so long."])
        for i, noun in enumerate(
            ["mill", "ferry", "orchard", "quarry", "smithy", "warehouse"])
    ]
    report = audit(_build(tmp_path, sections))
    phrases = _kinds(report, "pet-phrase")
    assert phrases, "recurring tic not flagged"
    joined = " ".join(f["phrase"] for f in phrases)
    assert "comes down to patience" in joined
    # The tic must be reported as one maximal span, not a chain of windows.
    best = max(phrases, key=lambda f: f["words"])
    assert len(best["detail"].split()) > max(
        __import__("manuscript_audit").PET_NGRAM_SIZES)


def test_uniform_section_length_detected(tmp_path):
    """Machine-even section lengths read as generated, not written."""
    sections = [(f"Chapter {i + 1}", list(VARIED)) for i in range(6)]
    report = audit(_build(tmp_path, sections))
    assert _kinds(report, "uniform-section-length")
    assert _kinds(report, "uniform-paragraph-count")


def test_uneven_sections_not_flagged_as_uniform(tmp_path):
    """Genuinely varied section lengths must pass."""
    sections = [(f"Chapter {i + 1}", VARIED[: (i % 3) + 1]) for i in range(6)]
    report = audit(_build(tmp_path, sections))
    assert not _kinds(report, "uniform-section-length")


def test_sentence_initial_capital_is_not_a_proper_noun(tmp_path):
    """"Dust hung over the valley" is weather, not a character.

    Every sentence starts capitalised, so capitalisation alone cannot mark a
    proper noun; treating it as one flagged most ordinary prose as the
    "invented character" opener.
    """
    sections = [(f"Chapter {i + 1}", list(VARIED)) for i in range(6)]
    secs = read_sections(_build(tmp_path, sections))
    names = collect_proper_nouns(secs)
    assert "Dust" not in names
    assert "Rain" not in names
    assert opener_shape(VARIED[0], names) != "proper-noun opener"


def test_midsentence_capital_counts_as_proper_noun(tmp_path):
    """A word capitalised away from a sentence start is a real name."""
    # Must clear paragraph_looks_like_body's threshold (>=2 sentences), or the
    # audit correctly treats it as an outline stub and never reads it.
    sections = [
        ("Chapter 1", ["The road west of Vermont ran badly that year, and the "
                       "party lost two days to it before the weather turned. "
                       "They camped short of the ridge and waited out the rain "
                       "with the wagons drawn up against the wind."]),
    ]
    secs = read_sections(_build(tmp_path, sections))
    assert "Vermont" in collect_proper_nouns(secs)


def test_front_matter_excluded_from_sections(tmp_path):
    """Body-prose gating keeps captions and stubs out of the audit."""
    doc = Document()
    doc.add_paragraph("Copyright 2026")          # front matter, no heading
    doc.add_heading("Chapter 1", level=1)
    for para in VARIED:
        doc.add_paragraph(para)
    path = tmp_path / "fm.docx"
    doc.save(str(path))
    report = audit(path)
    assert report["sections"] == 1


def test_empty_document_is_safe(tmp_path):
    """An empty or heading-only doc must not raise."""
    doc = Document()
    doc.add_heading("Chapter 1", level=1)
    path = tmp_path / "empty.docx"
    doc.save(str(path))
    report = audit(path)
    assert report["findings"] == []
    assert report["paragraphs"] == 0

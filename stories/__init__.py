"""Researched-stories book generator.

Books built from real, researchable events rather than invented fiction:
"World's Dumbest Criminals", "Amazing Cat Stories", "Record-Breaking Fishing
Tales". The operator supplies an outline of story titles plus a free-form
context box for each one, and the pipeline researches and writes a 300-500 word
micro-story per entry.

The context box is deliberately unstructured. Some outlines carry a full who /
year / where / sources breakdown; others carry one line ("the tuna caught off
Florida"). Both must work, so nothing here requires structured fields.

A self-contained subsystem: content engine, build pipeline, exporters, the
Preview & Edit layer, and its Flask routes. Nothing is shared with the prose or
trivia pipelines beyond the four root helpers it borrows (db,
openclaw_image_maker, openclaw_docx_writer, kdp_docx_formatter).

Wired into the app with a single call, matching trivia.py and friends:

    import stories
    stories.register(app)
"""

from __future__ import annotations

from .routes import register

__all__ = ["register"]

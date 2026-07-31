"""Puzzle & Activity book generator.

A self-contained subsystem implementing the Puzzle/Activity Book Production
Spec: content engine, in-house puzzle generators, build pipeline, exporters,
and its Flask routes. Nothing here is shared with the prose-book pipeline in
app.py or the trivia pipeline beyond the two root helpers it borrows
(db, kdp_docx_formatter).

Notably, the maze and crossword grids are generated in-house rather than
through the third-party services the spec named, so no membership window or
manual export step gates a build.

Wired into the app with a single call, matching trivia.py and friends:

    import puzzle
    puzzle.register(app)
"""

from __future__ import annotations

from .routes import register

__all__ = ["register"]

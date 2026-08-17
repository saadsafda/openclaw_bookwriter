"""Trivia & Facts book generator.

A self-contained subsystem: content engine, build pipeline, exporters, the
Preview & Edit layer, and its Flask routes. Nothing here is shared with the
prose-book pipeline in app.py beyond the four root helpers it borrows
(db, openclaw_image_maker, openclaw_docx_writer, kdp_docx_formatter).

Wired into the app with a single call, matching publications.py and friends:

    import trivia
    trivia.register(app)
"""

from __future__ import annotations

from .routes import register

__all__ = ["register"]

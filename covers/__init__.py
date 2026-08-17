"""Book cover generation — 10 style variations from a proven reference set.

A self-contained subsystem shared by all four book types (prose, trivia,
puzzle, stories). The book type only changes a wording fragment inside the
prompt; everything else — the rules block, the reference library, the
generation loop, print hygiene — is common.

The reference image is the whole trick. ``images.edit`` accepts a list of
source images and transfers their palette, illustration style, and
composition onto the new title, so ten proven covers yield ten distinct but
on-brand variations. (This is a style transfer, not a cache workaround: the
API is stateless and repeating a prompt already varies the output.)

Wired into the app with a single call, matching trivia/puzzle/stories:

    import covers
    covers.register(app)
"""

from __future__ import annotations

from .routes import register

__all__ = ["register"]

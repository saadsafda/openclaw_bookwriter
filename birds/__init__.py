"""Bird illustration pipeline — photo in, transparent 300 DPI illustration out.

The bird guides need 200-300 species each, across roughly 70 books. Doing that
by hand in a chat window does not survive the per-account image caps, and every
result has to be re-checked for a background that was supposed to be removed.
This module does the same job through the API, in batches, with the two things
the manual route keeps getting wrong made structural:

  * transparency is requested at the API level (``background="transparent"``),
    not merely asked for in the prompt, and is verified on the way out;
  * every finished PNG goes through ``print_hygiene`` so it is exactly 300 DPI
    with no EXIF/XMP/C2PA provenance chunk.

The freelancer's photos are the reference — ``images.edit`` keeps the species'
real plumage colors and markings rather than inventing a generic bird.

Wired into the app with a single call, matching trivia/puzzle/stories/covers:

    import birds
    birds.register(app)
"""

from __future__ import annotations

from .routes import register

__all__ = ["register"]

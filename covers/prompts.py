"""Cover prompt construction — the type-aware half of the covers module.

Two ways to reach a prompt:

  * :func:`build_prompt` fills the template directly from title/topic and is
    what runs when no API key is available or the operator declines the draft
    step. Deterministic, free, no network.
  * :func:`draft_prompt` asks the model to write the "what this book is about"
    sentence for a specific title, which is the step the operator was doing by
    hand in a long-running chat. It falls back to :func:`build_prompt` on any
    failure, so the dashboard always has a prompt to show.

Either way the operator sees the result in an editable box before any image is
generated, and can overwrite it entirely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 6x9 inches is the KDP trade paperback the whole pipeline targets; as an
# aspect ratio that is 2:3 portrait, which the image API expresses as
# 1024x1536. Stated both ways in the prompt because the model responds to the
# physical description as well as the ratio.
ASPECT_CLAUSE = "6x9 inch aspect ratio (portrait, 2:3)"

# Constant across every book type. Kept verbatim from the operator's own
# working prompt: art overlapping the title is the failure that makes a cover
# unusable, and stray text is the one the model reaches for unprompted.
RULES = (
    "Rules:\n"
    " - No humans\n"
    " - No text except the title\n"
    " - Art must not touch, cover, or overlap the title"
)

STYLE_CLAUSE = "Style: similar to the attached cover"


@dataclass(frozen=True)
class BookTypeProfile:
    """Per-type wording. Only ``description`` and ``art_hint`` vary."""

    key: str
    label: str
    description: str
    art_hint: str
    default_audience: str


# The description sentences mirror how each generator already describes itself
# (see puzzle/engine.py and trivia/engine.py defaults), so a cover reads as
# part of the same series as the book it fronts.
PROFILES: dict[str, BookTypeProfile] = {
    "puzzle": BookTypeProfile(
        key="puzzle",
        label="Puzzle Book",
        description=(
            "This is a funny gift puzzle book for {audience}, so make it "
            "playful, colorful, and charming."
        ),
        art_hint="Add some art of {topic} and puzzle elements.",
        default_audience="kids ages 8-12",
    ),
    "trivia": BookTypeProfile(
        key="trivia",
        label="Trivia & Facts Book",
        description=(
            "This is a fun facts and trivia book about {topic} for "
            "{audience}, so make it bright, curious, and inviting."
        ),
        art_hint="Add some art of {topic} and playful trivia elements.",
        default_audience="general adult reader",
    ),
    "stories": BookTypeProfile(
        key="stories",
        label="Story Collection",
        description=(
            "This is a story collection about {topic} for {audience}, so make "
            "it warm, characterful, and story-like."
        ),
        art_hint="Add some illustrative art evoking {topic}.",
        default_audience="general reader",
    ),
    "book": BookTypeProfile(
        key="book",
        label="Book (prose)",
        description=(
            "This is a non-fiction book about {topic} for {audience}, so make "
            "it clean, confident, and professional."
        ),
        art_hint="Add some tasteful art evoking {topic}.",
        default_audience="general adult reader",
    ),
}

DEFAULT_TYPE = "puzzle"


def profile_for(book_type: str) -> BookTypeProfile:
    return PROFILES.get((book_type or "").strip().lower(), PROFILES[DEFAULT_TYPE])


def normalize_title(title: str) -> str:
    """Collapse whitespace and uppercase — the title is rendered all caps."""
    cleaned = re.sub(r"\s+", " ", (title or "").strip())
    return cleaned.upper()


def build_prompt(
    title: str,
    book_type: str = DEFAULT_TYPE,
    topic: str = "",
    audience: str = "",
) -> str:
    """Fill the cover template directly. No network, always succeeds."""
    prof = profile_for(book_type)
    display_title = normalize_title(title)
    if not display_title:
        raise ValueError("A book title is required to build a cover prompt.")

    # Topic drives the art hint. Falling back to the title keeps the sentence
    # readable when the caller has no separate topic field.
    topic_text = (topic or "").strip() or display_title.title()
    audience_text = (audience or "").strip() or prof.default_audience

    description = prof.description.format(topic=topic_text, audience=audience_text)
    art_hint = prof.art_hint.format(topic=topic_text)

    return (
        f'Book cover, {ASPECT_CLAUSE}. The title "{display_title}" in bold '
        f"all caps as the main focus. {description} {art_hint}\n\n"
        f"{STYLE_CLAUSE}\n\n"
        f"{RULES}"
    )


_DRAFT_SYSTEM = (
    "You write the one- or two-sentence description used inside a book-cover "
    "image prompt. Given a book title and type, state what the book is about "
    "and the mood the cover should have. Name concrete objects an illustrator "
    "could draw. Do not mention the title text, typography, layout, humans, or "
    "any rule about text placement — those are handled elsewhere. Reply with "
    "the sentences only, no preamble."
)


def draft_prompt(
    title: str,
    book_type: str = DEFAULT_TYPE,
    topic: str = "",
    audience: str = "",
    api_key: str = "",
) -> tuple[str, bool]:
    """Draft the description with the model, then assemble the full prompt.

    Returns ``(prompt, drafted)``. ``drafted`` is False when the template
    fallback was used, so the dashboard can show which path produced the text.
    Any failure falls back rather than raising: a cover prompt the operator can
    edit is always more useful than an error page.
    """
    base = build_prompt(title, book_type, topic, audience)
    if not api_key:
        return base, False

    prof = profile_for(book_type)
    display_title = normalize_title(title)
    topic_text = (topic or "").strip() or display_title.title()
    audience_text = (audience or "").strip() or prof.default_audience

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _DRAFT_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Title: {display_title}\n"
                        f"Book type: {prof.label}\n"
                        f"Topic: {topic_text}\n"
                        f"Audience: {audience_text}"
                    ),
                },
            ],
            max_tokens=160,
        )
        description = (response.choices[0].message.content or "").strip()
    except Exception:
        return base, False

    if not description:
        return base, False

    return (
        f'Book cover, {ASPECT_CLAUSE}. The title "{display_title}" in bold '
        f"all caps as the main focus. {description}\n\n"
        f"{STYLE_CLAUSE}\n\n"
        f"{RULES}"
    ), True

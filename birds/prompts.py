"""Bird illustration prompt construction.

The wording here is the operator's own working prompt from the manual run,
tightened into a template:

    "Create a colored illustration of this bird in 300 DPI that can be used in
     a professional book, and don't have any background around it, just the
     bird."

That sentence is the whole prompt, used **verbatim** and on its own. The
reference photo is doing the rest of the work: the model can already see the
species, its colors, its markings and its pose, so restating any of that in
words adds nothing and only gives the output room to drift from wording that
already works.

The one optional addition is a style sentence for the non-default looks
(watercolor, vector, vintage). The default sends the base prompt alone.

Note that the background instruction inside the prompt is *not* what produces
the alpha channel — ``background="transparent"`` on the API call is. The
sentence still matters: it stops the model painting a white rectangle
underneath the transparency, which the parameter alone does not prevent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The operator's own working prompt, verbatim. This exact wording is what
# produced the results they approved in the manual run, so it is deliberately
# not paraphrased, expanded, or "improved" — changes here change every plate in
# every guide.
#
# It carries the 300 DPI phrasing even though print_hygiene is what actually
# pins the file, and the background instruction even though
# ``background="transparent"`` is what actually produces the alpha channel.
# Both stay: the wording nudges the model toward clean high-detail linework and
# away from painting a backdrop under the transparency.
BASE_PROMPT = (
    "Create a Colored illustration of this bird in 300 DPI that can be used "
    "in a professional book, and don't have any background around it, just "
    "the bird."
)


@dataclass(frozen=True)
class StyleProfile:
    """One illustration look. A whole guide is generated in a single style."""

    key: str
    label: str
    clause: str


# The default adds nothing: BASE_PROMPT already asks for a colored
# illustration for a professional book, and appending a second style sentence
# would only give the model a chance to drift from wording that already works.
# The alternatives are opt-in looks a bird guide realistically ships in.
PROFILES: dict[str, StyleProfile] = {
    "field_guide": StyleProfile(
        key="field_guide",
        label="Colored Illustration (default)",
        clause="",
    ),
    "watercolor": StyleProfile(
        key="watercolor",
        label="Watercolor",
        clause=(
            "a soft colored watercolor illustration with natural pigment "
            "washes and delicate feather detail, clean edges, no paper texture "
            "behind the bird"
        ),
    ),
    "vector": StyleProfile(
        key="vector",
        label="Flat Vector",
        clause=(
            "a clean flat vector-style illustration with smooth shapes and "
            "simple color blocking, minimal shading, crisp edges"
        ),
    ),
    "vintage": StyleProfile(
        key="vintage",
        label="Vintage Audubon",
        clause=(
            "a vintage hand-colored engraving-style illustration in the "
            "tradition of classic 19th-century ornithological plates, with "
            "fine linework and muted natural color"
        ),
    ),
}

DEFAULT_STYLE = "field_guide"


def profile_for(style: str) -> StyleProfile:
    return PROFILES.get((style or "").strip().lower(), PROFILES[DEFAULT_STYLE])


def species_from_filename(filename: str) -> str:
    """Best-effort species name from the freelancer's filename.

    The photo set arrives named after the birds ("northern_cardinal_02.jpg"),
    so the name is usually right there. A trailing counter is dropped, since
    "Northern Cardinal 02" is not a species. This is only ever a *default* the
    operator can overwrite per bird, so a wrong guess is cheap.
    """
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", (filename or "").strip())
    stem = re.sub(r"[_\-]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    # Drop a trailing index ("cardinal 02", "cardinal (3)") but keep a name
    # that is genuinely numeric-free.
    stem = re.sub(r"[\s(\[]*\d+[\s)\]]*$", "", stem).strip()
    if not stem:
        return ""
    return " ".join(word.capitalize() for word in stem.split())


def build_prompt(species: str = "", style: str = DEFAULT_STYLE, notes: str = "") -> str:
    """Assemble the illustration prompt. No network, always succeeds.

    The prompt is the operator's own working sentence, used verbatim and
    nothing else. The reference photo carries the rest — species, colors, pose
    — so restating any of it in words is redundant at best and a source of
    drift at worst.

    ``species`` is accepted and deliberately unused: the caller tracks it per
    bird for filenames, captions and the DOCX, and keeping it in the signature
    means that plumbing does not change if it is ever wanted in the text again.
    """
    parts = [BASE_PROMPT]

    # Only the opt-in styles add anything. The default is the base prompt
    # alone.
    prof = profile_for(style)
    if prof.key != DEFAULT_STYLE and prof.clause.strip():
        parts.append(f"Render it as {prof.clause}.")

    extra = (notes or "").strip()
    if extra:
        parts.append(extra)

    return " ".join(parts)

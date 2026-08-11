"""Bird illustration prompt construction.

The wording here is the operator's own working prompt from the manual run,
tightened into a template:

    "Create a colored illustration of this bird in 300 DPI that can be used in
     a professional book, and don't have any background around it, just the
     bird."

Two changes to that text, both load-bearing:

  * "same colors as the photograph" is stated explicitly. The point of sending
    the reference photo is species accuracy — a Northern Cardinal that comes
    back orange is useless in a field guide, and the model will drift without
    being told not to.
  * the background instruction is repeated in the prompt even though
    ``background="transparent"`` is also passed to the API. The API parameter
    is what actually produces the alpha channel; the prompt sentence stops the
    model from *painting* a white rectangle underneath it, which the parameter
    alone does not prevent.

Style presets exist because a guide has to look like one book. Whichever style
a given series picks, all 200-300 plates in it use the same one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Repeated in the prompt text even though print_hygiene is what actually pins
# the file to 300 DPI. The phrase reliably nudges the model toward clean,
# high-detail linework rather than a soft web-sized sketch.
DPI_CLAUSE = "at 300 DPI, print quality, suitable for a professional printed book"

# The failure that makes a plate unusable is a background the cutout has to
# fight: a painted white box, a vignette, a shadow, a perch the bird is
# standing on. Each of those is called out by name because the model adds them
# unprompted when asked only for "no background".
ISOLATION_RULES = (
    "Rules:\n"
    " - Just the bird, completely isolated on a fully transparent background\n"
    " - No background scenery, sky, foliage, branch, or perch\n"
    " - No ground shadow, drop shadow, vignette, or painted backdrop\n"
    " - No text, labels, watermarks, borders, or frames\n"
    " - One single bird only, whole body, nothing cropped off"
)

# Species accuracy is the whole reason a reference photo is attached.
FIDELITY_CLAUSE = (
    "Match the exact plumage colors, markings, proportions, and beak and leg "
    "color of the bird in the photograph."
)


@dataclass(frozen=True)
class StyleProfile:
    """One illustration look. A whole guide is generated in a single style."""

    key: str
    label: str
    clause: str


# Wording mirrors how the operator described the target in the video ("colored
# illustration ... professional book"), with the alternatives being the looks a
# bird guide realistically ships in.
PROFILES: dict[str, StyleProfile] = {
    "field_guide": StyleProfile(
        key="field_guide",
        label="Field Guide Illustration",
        clause=(
            "a detailed colored field-guide illustration, in the classic "
            "naturalist style of a printed bird identification guide, with "
            "crisp feather detail and clean edges"
        ),
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

    ``species`` is optional: with the photo attached the model can already see
    the bird, so naming it is a correctness aid rather than a requirement.
    """
    prof = profile_for(style)
    name = (species or "").strip()
    subject = f"this bird ({name})" if name else "this bird"

    parts = [
        f"Create {prof.clause} of {subject}, {DPI_CLAUSE}.",
        FIDELITY_CLAUSE,
    ]

    extra = (notes or "").strip()
    if extra:
        parts.append(extra)

    return " ".join(parts) + "\n\n" + ISOLATION_RULES

"""Local background removal — the bird's own pixels, no redraw, no API.

This is the default path. The operator wants the photograph's own bird with
its background deleted: same feathers, same colors, same pose. That is a
segmentation job, not a generation job, so it runs locally against a U²-Net
model (``rembg``) instead of costing an API call per bird. At 200-300 birds
across ~70 guides the difference is roughly 20,000 paid calls versus none.

**Why a subprocess.** ``rembg`` depends on ``onnxruntime``, ``pymatting`` and
``numba``, none of which ship wheels for the Python 3.13 the app runs on. They
install cleanly on 3.11, so the removal runs in a sidecar interpreter and the
app talks to it over argv and the filesystem. The alternative — pinning the
whole application back to 3.11 — would be a far larger change for one feature.

The sidecar is found once and cached. When it is missing, the error names the
exact command that creates it rather than failing with an import trace.
"""

from __future__ import annotations

import json
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

# Where setup puts the 3.11 interpreter. Checked in order; the first that can
# import rembg wins.
SIDECAR_CANDIDATES = (
    ROOT_DIR / ".venv_rembg" / "bin" / "python",
    ROOT_DIR / ".venv_rembg" / "Scripts" / "python.exe",   # Windows
)

# u2net is the general-purpose model and is what was measured on real bird
# photos: it removed the perch branch cleanly, which the classical
# (GrabCut) approach did not. isnet-general-use is a drop-in alternative.
DEFAULT_MODEL = "u2net"
VALID_MODELS = {"u2net", "u2netp", "isnet-general-use", "birefnet-general"}

# A single 1536px bird takes ~0.5s warm. Sixty seconds is generous enough to
# absorb the one-time model download on the very first call.
TIMEOUT_S = 600

SETUP_HINT = (
    "Local background removal needs its own Python 3.11 environment. "
    "Create it with:\n"
    "  python3.11 -m venv .venv_rembg\n"
    "  .venv_rembg/bin/pip install --no-deps rembg pymatting\n"
    "  .venv_rembg/bin/pip install onnxruntime pillow numpy scipy tqdm "
    "scikit-image jsonschema pooch\n"
    "  .venv_rembg/bin/pip install --only-binary=:all: numba llvmlite"
)


class CutoutError(RuntimeError):
    """Raised when local background removal cannot run or fails on an image."""


# The worker. Kept as a string and run with -c so there is no second file to
# keep in sync with this module, and no import path juggling in the sidecar.
_WORKER = r"""
import json, sys
from PIL import Image
from rembg import remove, new_session

src, dest, model = sys.argv[1], sys.argv[2], sys.argv[3]
session = new_session(model)
with Image.open(src) as img:
    out = remove(img.convert("RGB"), session=session, post_process_mask=True)
out.save(dest, format="PNG")
alpha = out.getchannel("A")
lo, hi = alpha.getextrema()
bbox = alpha.getbbox()
print(json.dumps({
    "ok": True,
    "size": list(out.size),
    "alpha_min": lo,
    "alpha_max": hi,
    "bbox": list(bbox) if bbox else None,
}))
"""


@lru_cache(maxsize=1)
def find_sidecar() -> Path | None:
    """The 3.11 interpreter that can import rembg, or None."""
    for candidate in SIDECAR_CANDIDATES:
        if not candidate.is_file():
            continue
        try:
            probe = subprocess.run(
                [str(candidate), "-c", "import rembg"],
                capture_output=True,
                timeout=120,
            )
            if probe.returncode == 0:
                return candidate
        except (subprocess.SubprocessError, OSError):
            continue
    return None


def is_available() -> bool:
    return find_sidecar() is not None


def remove_background(
    source: Path,
    dest: Path,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Cut ``source`` out onto transparency, writing PNG to ``dest``.

    Returns the worker's report (size, alpha range, bbox). Raises
    :class:`CutoutError` with an actionable message on any failure.
    """
    if model not in VALID_MODELS:
        model = DEFAULT_MODEL

    python = find_sidecar()
    if python is None:
        raise CutoutError(SETUP_HINT)

    source, dest = Path(source), Path(dest)
    if not source.is_file():
        raise CutoutError(f"Source photo is missing: {source}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        proc = subprocess.run(
            [str(python), "-c", _WORKER, str(source), str(dest), model],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise CutoutError(
            f"Background removal timed out after {TIMEOUT_S}s."
        ) from exc
    except OSError as exc:
        raise CutoutError(f"Could not run background removal: {exc}") from exc

    if proc.returncode != 0:
        # rembg's own stderr is the useful part; the last line is usually the
        # actual exception.
        detail = (proc.stderr or "").strip().splitlines()
        raise CutoutError(detail[-1] if detail else "Background removal failed.")

    if not dest.is_file():
        raise CutoutError("Background removal produced no output file.")

    # The worker prints one JSON line last; tqdm may have written progress
    # before it, so parse from the end.
    for line in reversed((proc.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                break
    return {"ok": True}

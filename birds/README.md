# Bird guide pipeline

Photos in → transparent 300 DPI PNGs → DOCX book. Reachable at `/birds`.

## Two output modes

| | Cut out background (default) | Redraw as illustration |
|---|---|---|
| What you get | Your photo's own bird, background deleted | AI-drawn artwork of the bird |
| Pixels | Identical to the original photo | Regenerated |
| Pose / colors | Unchanged by construction | Guided by the prompt |
| Cost | **Free** — runs locally | One API call per bird |
| Speed | ~0.5 s per bird | Several seconds per bird |

Cut-out mode is the default. It uses the **full-resolution original** you
uploaded; illustration mode uses the 1536 px reference the API consumes anyway.

## Cut-out mode needs a Python 3.11 sidecar

`rembg` depends on `onnxruntime`, `pymatting` and `numba`, none of which ship
wheels for the Python 3.13 the app runs on. They install fine on 3.11, so
background removal runs in a separate interpreter that the app calls as a
subprocess (see `cutout.py`).

```bash
python3.11 -m venv .venv_rembg
.venv_rembg/bin/pip install --no-deps rembg pymatting
.venv_rembg/bin/pip install onnxruntime pillow numpy scipy tqdm \
    scikit-image jsonschema pooch
.venv_rembg/bin/pip install --only-binary=:all: numba llvmlite
```

`--no-deps` on the first line and `--only-binary` on the last are both
load-bearing: without them pip tries to compile `llvmlite` from source, which
fails. The model (~176 MB) downloads itself on first use and is then cached.

If the sidecar is missing the page shows these instructions instead of the
cut-out option, and the API refuses the run with the same text rather than
failing per-bird.

## Layout

| File | Role |
|---|---|
| `library.py` | Photo intake, per-guide batches. Keeps originals untouched. |
| `cutout.py` | Local background removal via the 3.11 sidecar. |
| `engine.py` | Run loop, transparency verification, trim, 300 DPI. |
| `prompts.py` | Illustration prompt (illustration mode only). |
| `export.py` | DOCX book builder — one bird per page. |
| `routes.py` | Flask routes and the background job runner. |

## Things that are structural, not incidental

- **Originals are never re-encoded.** `bird_sources/<batch>/originals/` holds
  your upload byte-for-byte; the downscaled PNG beside it is only an API
  reference. Verified by SHA-256.
- **Transparency is verified, not assumed.** A plate that comes back opaque is
  flagged rather than silently shipped, and is excluded from the DOCX by
  default — a background that survived would print as a grey box.
- **Plates are flattened onto white for the DOCX**, into a temp copy. Word
  composites transparency unpredictably in print. The transparent originals on
  disk are untouched.

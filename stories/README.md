# Stories pipeline

Books built from **real, researchable events** rather than invented fiction —
*World's Dumbest Criminals*, *Amazing Cat Stories*, *Record-Breaking Fishing
Tales*. The operator supplies an outline of story titles plus a free-form
context box for each one, and the pipeline writes a 300–500 word micro-story per
entry.

Self-contained, like `trivia/` and `puzzle/`. Wired in with one call in
`app.py`:

```python
import stories
stories.register(app)
```

Everything lives under `/stories` and `/api/stories/*`.

## The core idea: a title plus a context box

The context box is deliberately **unstructured**. Outlines vary enormously:

| Outline style | What the operator has | What the pipeline does |
|---|---|---|
| Rich | Who / Year / Where / full summary / sources | Treats it as verified ground truth and expands it into prose |
| Thin | *"the tuna caught off Florida"* | Works from what the model knows about the real event, and flags every uncertain detail |

Both paths are first-class. `StoryConfig.context_block()` emits only the fields
that are actually filled in, so a thin outline never produces `Year: (unknown)`
lines that invite the model to invent a filler value.

Because these books are sold as **true** stories, the prompt forbids inventing
names, dates, amounts, locations and quotes in both modes, and every story
returns a `uncertain_claims` list that feeds the fact-check sheet.

## Files

| File | Role |
|---|---|
| `engine.py` | Config and content models, OpenClaw plumbing, prompts, quality checks, dedup |
| `pipeline.py` | Build orchestration, retries, illustrations, export gate, JSON round-trip |
| `outline.py` | Parses an existing `.docx` / `.txt` / `.md` outline into story entries |
| `export.py` | Markdown, DOCX, KDP 6×9, and the fact-check sheet |
| `edit.py` | Manual + AI edits, regenerate, add story, post-edit validation |
| `image_edit.py` | Local (Pillow) and AI illustration edits, with undo history |
| `routes.py` | Flask routes and the background job runner |

Templates: `templates/stories.html` (build) and `templates/stories_edit.html`
(preview & edit). Table: `story_books`, with helpers in `db.py`.

## Build flow

1. **Outline in** — upload a `.docx`, paste text, or have the AI propose one
   (`/api/stories/suggest-outline`). The parser reads numbered headings,
   `Who:` / `Year:` / `Where:` / `Sources:` labels, and folds everything else
   into the context box.
2. **One call per story.** Stories are independent, so a hundred-story book
   never fails wholesale because story 63 went wrong.
3. **Quality gate** — length band, banned generic openers, opener repetition
   across the book, no bullet lists, no restating the title. Failures are
   retried with the reason fed back in (`MAX_STORY_ATTEMPTS = 4`).
4. **Graceful degradation** — if every attempt fails, the best attempt that
   still clears the 120-word absolute floor is kept and flagged. Nothing below
   the floor is kept, because the export gate rejects it and the whole book
   would be blocked over one bad story.
5. **Duplicate sweep** — reported, never auto-deleted: a near-duplicate usually
   means two outline entries covering one incident, which is an editorial call.
6. **Export** — JSON (source of truth), Markdown, fact-check sheet, DOCX, and
   KDP Kindle/paperback via the shared `kdp_docx_formatter`.

## Outputs

Alongside the usual manuscript files, every build produces a **fact-check
sheet** pairing each story with its researcher context, the sources the model
cited (marked unverified), and a checklist of claims needing verification. It is
the working document for the verification pass before publication.

## Tuning notes

- `NEAR_DUPLICATE_JACCARD = 0.45` — measured, not guessed. Stories about
  genuinely different events score ~0.0–0.08 even when they share topic
  vocabulary; a true retelling of the same incident in different words scores
  ~0.54. The threshold sits in the wide gap between those populations.
- `LENGTH_TOLERANCE = 0.12` — models routinely land slightly outside a stated
  band, and rejecting a 290-word story from a 300–500 band burns calls for no
  editorial gain.
- Illustrations are **off by default** and always wordless and people-free:
  these books cover real, named people, so a generated face would read as a
  depiction of that person.

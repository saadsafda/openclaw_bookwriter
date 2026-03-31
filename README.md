# OpenClaw Book Writer

Turn a `.docx` outline into a drafted, formatted, and optionally illustrated book.

## What It Does

1. Reads chapter headings/subheadings from your `.docx`.
2. Generates paragraph content with the OpenClaw CLI.
3. Saves progress after each heading (safe to resume).
4. Auto-formats the document for print-style layout.
5. Runs a clarity pass (Hemingway via Playwright) when available.
6. Optionally inserts one AI image per main heading.

## Requirements

### Required

- Python 3.10+
- OpenClaw CLI installed and configured
- `python-docx`

### Optional (based on features you use)

- `openai` + `Pillow` (image generation)
- `playwright` (clarity scrub automation)
- `flask` (web UI)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install python-docx openai pillow playwright flask
playwright install chromium
```

Optional `.env` in project root:

```env
OPENAI_API_KEY=your_key_here
```

## Quick Start (CLI)

### 1) Prepare your input `.docx`

Use heading lines for chapters/sections. Common patterns:

- `Chapter 1: ...`
- `Introduction`, `Conclusion`, `Epilogue`, `Foreword`, `Preface`
- Subheadings like `- Thing 1: ...`, `- Topic ...`, `1. Numbered item`, `Focus: ...`

### 2) Run the writer

Basic run:

```bash
python3 openclaw_docx_writer.py /path/to/book.docx --agent main
```

With images:

```bash
python3 openclaw_docx_writer.py /path/to/book.docx --agent main --images --image-model gpt-image-1
```

Write to a separate output file:

```bash
python3 openclaw_docx_writer.py /path/to/book.docx /path/to/output.docx --agent main
```

### 3) Final outputs

The pipeline can produce:

- `<name>.docx` (written progressively, unless custom output path is used)
- `<name>_formatted.docx`
- `<name>_formatted_clear.docx` (if clarity pass runs successfully)

If images are enabled, they are inserted into the latest final file.

## Most Useful Options

- `--force` re-generates existing content
- `--sleep <seconds>` slows requests between headings
- `--thinking <level>` sets OpenClaw thinking (`off|minimal|low|medium|high|xhigh`)
- `--timeout <seconds>` OpenClaw timeout (default `180`)
- `--image-heading "text"` regenerate image(s) only for matching heading(s)
- `--session-file .openclaw_session_id` controls persistent session storage

## Cache and Resume Behavior

- Text/image cache: `.openclaw_cache/`
- Session ID file: `.openclaw_session_id`

You can stop and rerun safely. Cached prompts are reused automatically unless `--force` is set.

## Web UI (Optional)

```bash
source .venv/bin/activate
python3 app.py
```

Open: `http://127.0.0.1:5000`

**For a full step-by-step walkthrough, see [WEB_UI_GUIDE.md](WEB_UI_GUIDE.md).**

## Standalone Utilities

Format only:

```bash
python3 format_docx.py input.docx [output.docx]
```

Clarity scrub only:

```bash
python3 clarity_agent.py --login
python3 clarity_agent.py /path/to/book_formatted.docx
```

Image only:

```bash
python3 openclaw_image_maker.py --heading "Chapter 1" --openai-api-key "$OPENAI_API_KEY" --output chapter1.png
```

## Project Files

- `openclaw_docx_writer.py` main pipeline (write + format + clarity + images)
- `format_docx.py` document styling
- `clarity_agent.py` Hemingway automation
- `openclaw_image_maker.py` image generation helper
- `app.py` Flask web interface

# OpenClaw Book Writer — User Manual

## 1. Requirements

| Requirement | Details |
|---|---|
| Python | `/opt/homebrew/bin/python3` (Homebrew Python 3) |
| `python-docx` | Installed via Homebrew Python pip |
| OpenClaw CLI | Installed and configured with a valid agent |

### Install python-docx (first time only)

```bash
/opt/homebrew/bin/python3 -m pip install python-docx
```

---

## 2. Quick Start (Full Workflow)

### Step 1 — Create your input .docx file

Open Microsoft Word (or LibreOffice) and write your book outline.  
See [Section 4](#4-how-to-write-your-input-file) for exact formatting rules.

Save it as, for example: `MyBook.docx`

### Step 2 — Run the writer

```bash
/opt/homebrew/bin/python3 openclaw_docx_writer.py /Users/mac/MyBook.docx --agent main \
  --words 250 --words-max 320 \
  --subwords 250 --subwords-max 320 \
  --tone "friendly, encouraging, and easy to understand"
```

The script will:
- Go through every heading and subheading in your outline
- Send each one to OpenClaw and get a paragraph written
- Insert the paragraph right after the heading in the document
- Save after every heading (so you never lose progress)
- **Automatically run the formatter** when finished → produces `MyBook_formatted.docx`

### Step 3 — Open your formatted book

Open `MyBook_formatted.docx` in Word. Your book is done.

---

## 3. openclaw_docx_writer.py — Reference

### Basic usage

```bash
/opt/homebrew/bin/python3 openclaw_docx_writer.py INPUT.docx --agent AGENT_NAME
```

### Arguments

| Argument | Required | Default | Description |
|---|---|---|---|
| `input` | Yes | — | Path to your input `.docx` outline |
| `--agent` | Yes | — | OpenClaw agent ID or name (e.g. `main`, `ops`) |
| `--words` | No | `160` | Minimum words per chapter/heading paragraph |
| `--words-max` | No | `words + 40` | Maximum words per chapter/heading paragraph |
| `--subwords` | No | `80` | Minimum words per bullet/subheading paragraph |
| `--subwords-max` | No | `subwords + 40` | Maximum words per bullet/subheading paragraph |
| `--tone` | No | `clear, professional, and engaging` | Writing tone for all paragraphs |
| `--force` | No | off | Re-generate ALL content, even already-written paragraphs |
| `--sleep` | No | `0.0` | Seconds to wait between API calls (use if hitting rate limits) |
| `--timeout` | No | `180` | Seconds before an OpenClaw call times out |
| `--thinking` | No | _(none)_ | OpenClaw thinking level: `off`, `minimal`, `low`, `medium`, `high`, `xhigh` |
| `--local` | No | off | Force OpenClaw to use local/embedded runtime |
| `--cache` | No | `.openclaw_cache` | Folder where AI responses are cached |
| `--session-id` | No | auto | OpenClaw session ID (auto-generated and persisted) |
| `--session-file` | No | `.openclaw_session_id` | File that stores the persistent session ID |

### Examples

```bash
# Basic run — 160–200 words per chapter, 80–120 per subheading
/opt/homebrew/bin/python3 openclaw_docx_writer.py input.docx --agent main

# Longer paragraphs — 250–300 words per chapter heading
/opt/homebrew/bin/python3 openclaw_docx_writer.py input.docx --agent main --words 250 --words-max 300

# Change tone
/opt/homebrew/bin/python3 openclaw_docx_writer.py input.docx --agent main --tone "warm, conversational, and humorous"

# Force re-generation of everything (ignore cache and existing content)
/opt/homebrew/bin/python3 openclaw_docx_writer.py input.docx --agent main --force

# Slow down requests to avoid rate limits
/opt/homebrew/bin/python3 openclaw_docx_writer.py input.docx --agent main --sleep 3

# Enable deeper thinking in OpenClaw
/opt/homebrew/bin/python3 openclaw_docx_writer.py input.docx --agent main --thinking medium
```

---

## 4. What the Formatted Output Looks Like

| Element | Style |
|---|---|
| Book Title | 32pt Georgia, bold, centered, navy (#1B3A5C), large spacer above |
| Subtitle | 14pt Georgia, italic, brown (#8B4513), centered |
| Cover divider | Single rule in brown |
| `CHAPTER N` label | 12pt Georgia, bold, brown, letter-spaced, new page |
| Chapter title | 22pt Georgia, bold, navy, centered |
| Chapter divider | Double rule in navy |
| `INTRODUCTION` / `CONCLUSION` | 22pt Georgia, bold, navy, ALL CAPS, centered, new page |
| `- Thing N: Title` | 14pt bold — number in brown, title in dark gray (#2D2D2D) |
| `- General subheading` | 14pt bold, navy |
| `Focus:` line | 12pt Georgia, italic, brown |
| Body paragraphs | 12pt Georgia, justified, 1.5 line spacing, first-line indent |
| Footer | `Book Title · Page N`, 8pt, gray, centered on every page |
| Page size | US Letter (8.5 × 11 in), 1-inch margins |

---

## 5. Caching — Resuming After Interruptions

Every AI response is saved as a `.txt` file in the `.openclaw_cache/` folder.  
The cache key is a hash of the exact prompt, so:

- If you re-run the script on the same file, it **reads from cache** (instant, no API call)
- If you change a heading text, it generates a **new** response for that heading only
- Use `--force` to **ignore the cache** and regenerate everything fresh
- **Delete the `.openclaw_cache/` folder** at any time to clear all cached content
---

*Generated for OpenClaw Book Writer — March 2026*

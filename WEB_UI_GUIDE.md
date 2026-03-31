# OpenClaw BookWriter — Web UI Guide

This guide walks you through using the BookWriter web interface step by step.
No terminal or coding knowledge required — everything happens in your browser.

---

## 1. Starting the Web UI

Open **Terminal** on the computer where BookWriter is installed and run:

```bash
cd /path/to/openclaw_book_genration
source .venv/bin/activate
python3 app.py
```

You should see output like:

```
 * Running on http://127.0.0.1:5000
```

Now open your browser and go to:

```
http://127.0.0.1:5000
```

> **Tip:** Keep the Terminal window open while you work. Closing it stops the server.

---

## 2. The Interface at a Glance

The screen has two areas:

| Area | What it does |
|---|---|
| **Left Sidebar** | Configuration panel — set writing options, image style, etc. |
| **Main Area** | Chat-style workspace — upload files, see live progress, download results |

---

## 3. Generating a Book (Step by Step)

### Step 1 — Prepare Your Layout File

Create a `.docx` file in Microsoft Word (or Google Docs → export as .docx).
Write your book structure using headings. Example:

```
Chapter 1: Getting Started
  - Setting Up Your Space
  - Finding Your Rhythm

Chapter 2: Building Habits
  - Morning Routines
  - Evening Wind-Down

Conclusion
```

**Rules:**
- Lines starting with `Chapter`, `Introduction`, `Conclusion`, `Epilogue`, `Foreword`, or `Preface` are treated as main chapter headings.
- Lines starting with `- `, `1. `, or `Focus:` are treated as subheadings.
- Don't write body text — the AI will generate it for you.

### Step 2 — Configure Settings (Left Sidebar)

| Setting | What it means | Default |
|---|---|---|
| **Writer Agent** | The OpenClaw agent to use for writing | `main` |
| **Writing Tone** | How the text should sound | `friendly, encouraging, and easy to understand` |
| **Min W/Heading** | Minimum words per chapter paragraph | `250` |
| **Max W/Heading** | Maximum words per chapter paragraph | `320` |
| **Generate Images** | Toggle ON to create illustrations for each chapter | ON |
| **Force Regenerate** | Check this to rewrite everything from scratch (ignores cache) | OFF |

Leave defaults if unsure — they work well for most books.

### Step 3 — Upload Your Layout File

**Option A — Upload from your computer:**
1. Click the **📎 paperclip** button at the bottom of the main area.
2. Select your `.docx` file.
3. The filename appears in the text bar.

**Option B — Paste a file path:**
1. If the `.docx` file is already on the server computer, paste the full path into the text bar.
   Example: `/Users/chaseopenclaw/openclaw_book_genration/web_uploads/MyBook.docx`

### Step 4 — Click "Write"

1. Click the blue **▶ Write** button.
2. A terminal output box appears showing live progress.
3. The status indicator in the header shows the current action.

**What happens behind the scenes:**
1. The AI reads each heading/subheading from your layout.
2. It generates a full paragraph for each one.
3. If images are enabled, it creates an illustration for each main chapter.
4. It formats the entire document for print-ready layout.
5. It saves the final file.

**This can take several minutes** depending on how many chapters you have.

### Step 5 — Download Your Book

When the job finishes (status shows ✅):

- Click **"Final File"** (blue button, top right) to download the completed book.
- Click **"Raw Output"** to download the unformatted version.

---

## 4. Replacing Images

After a book is generated, you can regenerate images for specific chapters
without rewriting the text.

### How to Replace Images

1. After the job completes, a purple **"Replace image..."** button appears at the bottom.
2. Click it — a popup shows all your chapter headings with checkboxes.
3. **Check the headings** you want new images for.
   - Use **Select All** to check everything.
   - Use **Clear** to uncheck everything.
4. Click **"Generate New Images"**.
5. The terminal output shows progress for each heading.
6. When done, download the updated file using the **"Final File"** button.

### Image Settings (Left Sidebar)

Before clicking "Generate New Images," you can change these in the sidebar:

| Setting | What it means | Default |
|---|---|---|
| **Style Variant** | The artistic style of illustrations | `rich-scene-no-text` |
| **Model** | AI image model to use | `gpt-image-1` |
| **Size** | Image dimensions | `1024x1536` |
| **Quality** | Image quality level | `high` |

#### Available Style Variants

| Variant | Description |
|---|---|
| `rich-scene-no-text` | Detailed storybook cartoon, no text in image, white background |
| `chapter-page-rich-gray` | Chapter opener page with heading text + rich cartoon below |
| `chapter-page-gray` | Chapter opener page with heading text + simple cartoon below |
| `black-gray` | Pure black & gray illustration, no text |
| `three-gray` | Minimal 3-shade grayscale illustration |

> **Tip:** `rich-scene-no-text` is the best default for most books. Use `chapter-page-rich-gray` if you want the chapter title baked into the image.

---

## 5. Understanding the Status Indicators

| Status | Meaning |
|---|---|
| 🔘 **Idle** | No job running, ready to start |
| 🔄 **generating_book** | Writing text and creating images |
| 🔄 **replacing_images** | Regenerating selected images |
| ✅ **success** | Job completed — download is ready |
| ❌ **error** | Something went wrong — check the terminal log |

---

## 6. Tips and Best Practices

### Getting Better Results

- **Be specific in your headings.** "Chapter 3: Morning Meditation Techniques" generates better content than "Chapter 3."
- **Use subheadings.** They break the chapter into focused sections.
- **Adjust word counts.** For shorter, punchier books, try `150`/`200`. For detailed guides, use `300`/`400`.
- **Try different tones.** Examples:
  - `warm, conversational, and humorous`
  - `professional, authoritative, and data-driven`
  - `gentle, supportive, and encouraging`

### Saving Time

- **Cached results are reused.** If you run the same book again, it skips headings that are already written (instant, no API cost).
- **Only replace images you don't like.** No need to regenerate all of them.
- **Use "Force Regenerate" sparingly.** It rewrites everything from scratch and costs more API calls.

### If Something Goes Wrong

| Problem | Solution |
|---|---|
| "No module named 'PIL'" | Run: `pip install Pillow` in the virtual environment |
| "OpenAI API key is required" | Add `OPENAI_API_KEY=sk-...` to the `.env` file in the project folder |
| Job stuck on "running" | Check the terminal where you started `app.py` for errors |
| Images look wrong | Try a different **Style Variant** and click "Replace image..." |
| File won't upload | Make sure it's a `.docx` file (not `.doc`, `.pdf`, or `.txt`) |
| "Command failed with exit code 1" | Check the terminal log in the chat — the error details are shown there |

---

## 7. File Locations

| File/Folder | Purpose |
|---|---|
| `web_uploads/` | Your uploaded `.docx` layout files |
| `web_outputs/` | Generated output files |
| `.openclaw_cache/` | Cached AI responses and images (safe to delete to start fresh) |
| `.env` | Your API key and settings (never share this file) |

---

## 8. Quick Reference

| Action | How |
|---|---|
| Start the server | `source .venv/bin/activate && python3 app.py` |
| Open the UI | Go to `http://127.0.0.1:5000` in your browser |
| Generate a book | Upload `.docx` → configure sidebar → click **Write** |
| Download result | Click **Final File** button (top right, after job completes) |
| Replace images | Click **Replace image...** → select headings → click **Generate New Images** |
| Stop the server | Press `Ctrl+C` in the Terminal window |

---

*OpenClaw BookWriter — Web UI Guide — March 2026*

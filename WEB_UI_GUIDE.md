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
- Click **"Kindle"** (top right) to download the Kindle-ready file (*_kindle.docx).
- Click **"Paperback"** (top right) to download the Paperback-ready file (*_paperback.docx).
- Click **"Raw Output"** to download the unformatted version.

If the TOC page shows **"Update this field to see Table of Contents."**, this is expected.
Open the downloaded `.docx` in Word, go to **References → Table of Contents**, and pick your preferred TOC style (for example, Automatic Table). Word will generate the full index on that page.

### Step 6 — Generate Publishing Listing

After the book is generated and formatted, you can create your KDP listing assets — subtitle ideas, the book description (also used on the paperback back cover), and Amazon category selections — all in one click.

1. After the job completes, an amber **"Pub Listing"** button appears at the bottom, next to the purple "Replace image..." button.
2. Click **"Pub Listing"**.
3. The terminal output box shows live progress as the agent works through three steps:
   - **Subtitle ideas** (5-10 options using proven frameworks)
   - **Book description** (170-220 words, ready for the Amazon listing and paperback back cover)
   - **Category selection** (3 Kindle + 3 Paperback categories from Amazon's full list)
4. When finished, a formatted result card appears in the chat showing:
   - **Subtitle Ideas** — numbered list of 5-10 options
   - **Book Description** — full description text with word count
   - **Ebook Categories (Kindle)** — 3 deep, long-tail categories from different top-level parents
   - **Paperback Categories** — 3 deep, long-tail categories from different top-level parents
5. Copy the results you want to use for your KDP listing.

> **Tip:** The description is written to work on both the Amazon product page and the paperback back cover. Run this step before designing the back cover so you have the text ready.

> **Note:** Categories follow two rules — they go as deep as possible in the Amazon category tree (long-tail) and each one is under a different top-level parent to maximize visibility across different shopper audiences.

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
| 🔄 **generating_listing** | Creating subtitles, description, and categories |
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

## 7. Kindle & Paperback Format Flow

When a book is generated, BookWriter automatically produces **two print-ready files**:

| File | Purpose |
|---|---|
| `*_kindle.docx` | Formatted for Kindle Direct Publishing (eBook) |
| `*_paperback.docx` | Formatted for KDP Paperback (print) |

### Kindle Format

The Kindle file is optimized for eBook readers:

| Page | Content | Layout |
|---|---|---|
| **Page 1** | Title page (book title, subtitle, author) | Vertically centered |
| **Page 2** | Copyright & legal notice | Vertically centered |
| **Page 3** | Free Bonus page | Vertically centered |
| **Page 4** | Table of Contents (Smart Identification) | Starts from top |
| **Page 5+** | Chapters with images | Starts from top |

- First 3 pages are **vertically centered** on the page.
- TOC and all body content start from the **top of the page**.
- Each chapter heading and its image are kept on the **same page**.
- Body text never appears on an image page — it starts on the next page.
- Large images are **automatically scaled** to fit within the page.

### Paperback Format

The Paperback file follows professional print book conventions:

| Page | Content | Position | Page Number |
|---|---|---|---|
| **Page 1** | Title page | Right (recto), centered | No |
| **Page 2** | Copyright | Left (verso), centered | No |
| **Page 3** | Free Bonus page | Right (recto), centered | No |
| **Page 4** | Table of Contents | Left/Right, top | No |
| **First chapter** | Chapter 1 heading + image | Right (recto), top | **Page 1** |

- **Right-page (recto) chapter starts:** Every chapter always begins on a right-hand page. Word will insert a blank page if needed.
- **Page numbering starts at Chapter 1** — not on front matter or TOC.
- **Odd pages** (right side) show page numbers on the right. **Even pages** (left side) show them on the left.
- **Gutter margin** is automatically calculated based on estimated page count (thicker books need wider gutters for binding).
- Heading + image are always on the **same page**. Body text starts on the next page.

---

## 8. Table of Contents (Smart Identification)

BookWriter uses a **native Word TOC field** — the same "Smart Identification" style available in Word's References tab. This means:

- ✅ **Clickable entries** — click any TOC line to jump to that chapter
- ✅ **Dot leaders + page numbers** — professional formatting
- ✅ **Multi-level** — Heading 1, 2, and 3 appear with proper indentation
- ✅ **Auto-updated** — Word rebuilds the TOC when you open the file

### What Gets Detected as a Heading

BookWriter uses smart identification to find headings automatically:

| Pattern | Example | Level |
|---|---|---|
| `Chapter X: Title` | Chapter 1: Getting Started | Heading 1 |
| `Chapter Word: Title` | Chapter One: The Journey | Heading 1 |
| `Part X` | Part I: The Beginning | Heading 1 |
| `Act X` | Act III — Climax | Heading 1 |
| `Book X` | Book Two: Return | Heading 1 |
| Front/back matter | Introduction, Conclusion, Epilogue, Prologue, Foreword, Preface | Heading 1 |
| Extended matter | Acknowledgments, About the Author, Dedication, Glossary, Appendix, Afterword, Bibliography | Heading 1 |
| Roman numerals | III. The Battle | Heading 1 |
| Numbered title | 1: The Beginning | Heading 1 |
| Bold/large/caps text | Short bold or ALL CAPS lines | Heading 1 |
| `Section X` | Section 3: Details | Heading 2 |
| Numbered sub | 1. Choosing the Right Venue | Heading 2 |
| Dash sub | - Setting Up Your Space | Heading 2 |

### How to Update the TOC in Word

When you open the generated `.docx` file in Microsoft Word:

1. If you see **"Update this field to see Table of Contents."** on the TOC page, the TOC field is waiting for Word to build the index.
2. Go to **References → Table of Contents** and choose your preferred TOC style (for example, Automatic Table).
3. If Word prompts for field updates, click **Yes**.
4. If the TOC is still not populated:
  - Click anywhere inside the TOC.
  - Right-click → **Update Field**.
  - Choose **"Update entire table"** → click **OK**.
5. The TOC will populate with all chapters, dot leaders, and correct page numbers.

Alternative quick method:

1. Word may prompt **"This document contains fields that may refer to other files. Do you want to update?"** — Click **Yes**.
2. If it doesn't auto-update:
   - Click anywhere inside the TOC.
   - Right-click → **Update Field**.
   - Choose **"Update entire table"** → click **OK**.
3. The TOC will populate with all chapters, dot leaders, and correct page numbers.

> **Tip:** Always update the TOC after making any edits to the document, so page numbers stay accurate.

### How to See the TOC in macOS Pages / Google Docs

- **macOS Pages:** Open the `.docx` file. Go to **View → Table of Contents** in the sidebar. Pages reads the heading styles and shows the TOC.
- **Google Docs:** Upload the `.docx` to Google Drive → Open with Google Docs. Go to **Insert → Table of contents** to re-insert it, or use the existing one. Click entries to navigate.
- **LibreOffice Writer:** Open the file. Right-click the TOC → **Update Index/Table**.

---

## 9. File Locations

| File/Folder | Purpose |
|---|---|
| `web_uploads/` | Your uploaded `.docx` layout files |
| `web_outputs/` | Generated output files |
| `.openclaw_cache/` | Cached AI responses and images (safe to delete to start fresh) |
| `.env` | Your API key and settings (never share this file) |

---

## 10. Quick Reference

| Action | How |
|---|---|
| Start the server | `source .venv/bin/activate && python3 app.py` |
| Open the UI | Go to `http://127.0.0.1:5000` in your browser |
| Generate a book | Upload `.docx` → configure sidebar → click **Write** |
| Download result | Click **Final File** button (top right, after job completes) |
| Replace images | Click **Replace image...** → select headings → click **Generate New Images** |
| Generate listing | Click **Pub Listing** → wait for results card → copy subtitles, description, categories |
| Stop the server | Press `Ctrl+C` in the Terminal window |

---

*OpenClaw BookWriter — Web UI Guide — April 2026*

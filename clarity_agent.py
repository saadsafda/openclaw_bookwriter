#!/usr/bin/env python3
"""
clarity_agent.py

Reads a .docx file, pastes its text into hemingwayapp.com via Playwright,
clicks every red/yellow highlighted sentence and uses Hemingway's built-in
"Simplify" → "Use suggestion" buttons to fix them automatically, then saves
the cleaned text back to a *_clear.docx.

This version drives the browser DIRECTLY with Playwright — no AI agent in the
loop — so it is 5-10× faster than the OpenClaw-agent approach.

Usage:
  # First run — log in once (browser stays open for you to sign in):
  python3 clarity_agent.py --login

  # All future runs — session is remembered, no login needed:
  python3 clarity_agent.py /path/to/book.docx
  python3 clarity_agent.py /path/to/book.docx --max-passes 5 --headless

Requires:
  pip install playwright python-docx
  playwright install chromium
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, TimeoutError as PwTimeout
from docx import Document

# Re-use the humanize helper from the writer module if available
try:
    from openclaw_docx_writer import humanize_text
except ImportError:
    def humanize_text(text: str) -> str:
        return text


# ── Constants ───────────────────────────────────────────────────────
HEMINGWAY_URL = "https://hemingwayapp.com"
RED_SELECTOR = "span.bg-red-200"         # "Very hard to read"
YELLOW_SELECTOR = "span.bg-yellow-200"   # "Hard to read"
SIMPLIFY_BTN_TEXT = "Simplify it for me"  # button text in popup
USE_SUGGESTION_TEXT = "Use suggestion"   # button text

# Persistent browser profile — stores cookies/localStorage so you only log in once
PROFILE_DIR = Path.home() / ".hemingway_playwright_profile"


class UpgradePlanRequired(Exception):
    """Raised when Hemingway shows an 'Upgrade plan' button, meaning the free
    AI usage limit has been hit. We stop fixing and save what we have."""
    pass


# ── Helpers ─────────────────────────────────────────────────────────

def extract_docx_text(path: Path) -> str:
    """Read all paragraph text from a .docx, preserving paragraph breaks."""
    doc = Document(str(path))
    paragraphs = []
    for p in doc.paragraphs:
        text = (p.text or "").strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def save_text_to_docx(text: str, output_path: Path) -> None:
    """Save cleaned plain text into a new .docx file."""
    doc = Document()
    doc.styles["Normal"].font.name = "Georgia"
    for para_text in text.split("\n\n"):
        para_text = para_text.strip()
        if para_text:
            doc.add_paragraph(para_text)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))



# ── Playwright automation ──────────────────────────────────────────

def dismiss_modal_dialogs(page: Page) -> None:
    """Dismiss any modal dialogs (e.g. welcome video) that block the editor."""
    try:
        # Look for open dialog overlays and close them
        dialog = page.locator("div[role='dialog'][data-state='open']")
        if dialog.count() > 0:
            # Try clicking a close button (X) inside the dialog
            close_btn = dialog.locator("button").filter(has_text=re.compile(r"^(×|✕|close|x|dismiss)$", re.I))
            if close_btn.count() > 0:
                close_btn.first.click(timeout=3000)
            else:
                # Try the generic close button (often first or last button, or one with aria-label)
                aria_close = dialog.locator("button[aria-label*='close' i], button[aria-label*='dismiss' i]")
                if aria_close.count() > 0:
                    aria_close.first.click(timeout=3000)
                else:
                    # Press Escape to dismiss
                    page.keyboard.press("Escape")
            time.sleep(0.5)
            print("  Dismissed modal dialog.", flush=True)
    except Exception:
        # If dismissal fails, try Escape as last resort
        try:
            page.keyboard.press("Escape")
            time.sleep(0.3)
        except Exception:
            pass


def paste_text_into_hemingway(page: Page, text: str) -> None:
    """Clear the Hemingway editor and paste text via clipboard."""
    # Dismiss any modal dialogs blocking the editor
    dismiss_modal_dialogs(page)

    # Click into the editor area
    editor = page.locator("[contenteditable='true']").first
    editor.click()
    time.sleep(0.5)

    # Select all + delete to clear default content
    page.keyboard.press("Meta+a")
    page.keyboard.press("Backspace")
    time.sleep(0.3)

    # Use clipboard to paste (much faster than typing)
    page.evaluate(f"navigator.clipboard.writeText({repr(text)})")
    page.keyboard.press("Meta+v")
    time.sleep(2)  # let Hemingway analyse


def fix_highlighted_sentences(
    page: Page,
    selector: str,
    label: str,
    max_fixes: int = 200,
    max_retries: int = 3,
    max_consecutive_failures: int = 3,
) -> int:
    """
    Find all spans matching `selector`, click each one, click "Simplify",
    then click "Use suggestion". Returns how many were fixed.

    Failed spans are skipped past (not retried forever), and after
    `max_consecutive_failures` failures in a row we assume the Simplify
    feature is unavailable (e.g. not logged in) and stop.
    """
    fixed = 0
    skipped = 0  # failed spans stay in the list; work past them by index
    consecutive_failures = 0
    for _ in range(max_fixes):
        spans = page.locator(selector)
        count = spans.count()
        if count == 0 or skipped >= count:
            break

        # Successful fixes shift the list; failed spans remain at the front.
        span = spans.nth(skipped)
        sentence_preview = (span.text_content() or "")[:60]
        print(f"    [{label}] Fixing: \"{sentence_preview}...\"", flush=True)

        retries_left = max_retries
        while retries_left > 0:
            try:
                span.click(timeout=3000)
                time.sleep(0.8)

                # ── Check for paywall before doing anything else ──
                upgrade_btn = page.get_by_role(
                    "button", name="Upgrade plan", exact=True
                )
                learn_more_btn = page.get_by_role(
                    "button", name="Learn more", exact=True
                )
                if upgrade_btn.is_visible() or learn_more_btn.is_visible():
                    print(
                        "  ⚠️  Paywall button detected ('Upgrade plan' or 'Learn more') — "
                        "free AI limit reached. Stopping fixes and saving.",
                        flush=True,
                    )
                    # Dismiss popup
                    page.locator("[contenteditable='true']").first.click()
                    raise UpgradePlanRequired()

                # Click "Simplify it for me" button in the popup
                simplify_btn = page.get_by_role("button", name=re.compile(SIMPLIFY_BTN_TEXT, re.I))
                simplify_btn.click(timeout=5000)
                time.sleep(1.5)

                # Click "Use suggestion"
                use_btn = page.get_by_role("button", name=re.compile(USE_SUGGESTION_TEXT, re.I))
                use_btn.click(timeout=5000)
                time.sleep(1.0)

                fixed += 1
                consecutive_failures = 0
                break
            except UpgradePlanRequired:
                raise  # propagate immediately — do not retry
            except (PwTimeout, Exception) as e:
                retries_left -= 1
                if retries_left > 0:
                    print(f"      Retry ({max_retries - retries_left}/{max_retries}): {e}", flush=True)
                    # Click elsewhere to dismiss any stuck popup
                    page.locator("[contenteditable='true']").first.click()
                    time.sleep(0.5)
                else:
                    print(f"      Skipping this sentence after {max_retries} retries.", flush=True)
                    # Click elsewhere to dismiss and move to the NEXT span
                    page.locator("[contenteditable='true']").first.click()
                    time.sleep(0.3)
                    skipped += 1
                    consecutive_failures += 1
                    break

        if consecutive_failures >= max_consecutive_failures:
            print(
                f"    [{label}] {consecutive_failures} sentences failed in a row — "
                "the 'Simplify it for me' button is not appearing. "
                "This usually means Hemingway is not logged in on this machine "
                "(run: python3 clarity_agent.py --login). Stopping fixes.",
                flush=True,
            )
            break

    return fixed


def get_editor_text(page: Page) -> str:
    """Select all and copy the editor text."""
    editor = page.locator("[contenteditable='true']").first
    editor.click()
    page.keyboard.press("Meta+a")
    time.sleep(0.3)
    # Get text via JS to avoid clipboard permission issues
    text = page.evaluate("""
        () => {
            const el = document.querySelector("[contenteditable='true']");
            return el ? el.innerText : '';
        }
    """)
    return (text or "").strip()


def process_document(
    page: Page,
    text: str,
    max_passes: int,
) -> str:
    """Paste the full document into Hemingway and fix all highlights. Returns cleaned text."""
    print(f"\n  Processing {len(text.split())} words in one pass.", flush=True)

    paste_text_into_hemingway(page, text)

    for pass_num in range(1, max_passes + 1):
        red_count = page.locator(RED_SELECTOR).count()
        yellow_count = page.locator(YELLOW_SELECTOR).count()
        total = red_count + yellow_count
        print(f"  Pass {pass_num}: {red_count} red, {yellow_count} yellow "
              f"({total} total)", flush=True)

        if total == 0:
            print(f"  ✓ All clear!", flush=True)
            break

        # Fix reds first, then yellows
        try:
            if red_count > 0:
                fixed = fix_highlighted_sentences(page, RED_SELECTOR, "RED")
                print(f"    Fixed {fixed} red sentences.", flush=True)
                if fixed == 0:
                    print(
                        "  No sentences could be fixed this pass — Simplify feature "
                        "unavailable (check Hemingway login/plan). Saving text as-is.",
                        flush=True,
                    )
                    break

            # yellow_count = page.locator(YELLOW_SELECTOR).count()
            # if yellow_count > 0:
            #     fixed = fix_highlighted_sentences(page, YELLOW_SELECTOR, "YELLOW")
            #     print(f"    Fixed {fixed} yellow sentences.", flush=True)
        except UpgradePlanRequired:
            # Save whatever text is in the editor right now, then re-raise.
            cleaned = get_editor_text(page)
            print(f"  Partial save ({len(cleaned.split())} words).", flush=True)
            raise UpgradePlanRequired(cleaned)

        time.sleep(1)  # let Hemingway re-analyse

    # Extract the cleaned text
    cleaned = get_editor_text(page)
    remaining_red = page.locator(RED_SELECTOR).count()
    remaining_yellow = page.locator(YELLOW_SELECTOR).count()
    print(f"  Done: {remaining_red} red, {remaining_yellow} yellow remaining.", flush=True)
    return cleaned


# ── CLI ─────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Clarity agent: automates Hemingway simplify/use-suggestion via Playwright"
    )
    ap.add_argument("input", nargs="?", type=str, default=None,
                    help="Input .docx file path (not needed with --login)")
    ap.add_argument("--output", type=str, default=None,
                    help="Output .docx path (default: <input>_clear.docx)")
    ap.add_argument("--max-passes", type=int, default=5,
                    help="Max editing passes (default: 5)")
    ap.add_argument("--headless", action="store_true",
                    help="Run browser in headless mode (no visible window)")
    ap.add_argument("--slow-mo", type=int, default=0,
                    help="Slow down actions by N ms (for debugging)")
    ap.add_argument("--login", action="store_true",
                    help="Open browser so you can log in to Hemingway. "
                         "Session is saved for all future runs.")
    args = ap.parse_args()

    # ── Login-only mode: open browser, let user sign in, then exit ──
    if args.login:
        print("Opening Hemingway so you can log in ...")
        print(f"  Profile saved to: {PROFILE_DIR}")
        print("  Log in, then close the browser window (or press Ctrl+C).\n")
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                headless=False,
                slow_mo=args.slow_mo,
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(HEMINGWAY_URL, wait_until="domcontentloaded", timeout=60000)
            print("  Browser is open. Log in now.")
            print("  When done, close the browser window.")
            try:
                page.wait_for_event("close", timeout=0)  # wait forever
            except Exception:
                pass
            context.close()
        print("\nLogin session saved! You can now run without --login.")
        return 0

    if not args.input:
        print("ERROR: No input file. Use --login to set up, or pass a .docx file.", file=sys.stderr)
        return 2

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"ERROR: File not found: {in_path}", file=sys.stderr)
        return 2

    out_path = Path(args.output) if args.output else in_path.with_stem(in_path.stem + "_clear")

    print(f"Input:      {in_path}")
    print(f"Output:     {out_path}")
    print(f"Max passes: {args.max_passes}")
    print(f"Headless:   {args.headless}")
    print()

    # Step 1: Extract text
    start = time.time()
    print("Step 1: Extracting text from .docx ...")
    text = extract_docx_text(in_path)
    word_count = len(text.split())
    print(f"  {word_count} words extracted.")
    if word_count == 0:
        print("ERROR: No text found in the document.", file=sys.stderr)
        return 3

    # Step 2: Launch browser and process the full document
    print("\nStep 2: Launching browser ...")
    full_cleaned = text  # fallback if something goes wrong

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        # Use persistent context so login cookies are saved & reused
        context = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=args.headless,
            slow_mo=args.slow_mo,
            permissions=["clipboard-read", "clipboard-write"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(HEMINGWAY_URL, wait_until="domcontentloaded", timeout=60000)
        # Wait for the editor to actually appear before proceeding
        page.wait_for_selector("[contenteditable='true']", timeout=30000)
        print(f"  Hemingway loaded (using saved session).\n")

        try:
            full_cleaned = process_document(
                page, text, max_passes=args.max_passes,
            )
        except UpgradePlanRequired as exc:
            full_cleaned = exc.args[0] if exc.args else text
            print("  Paywall hit — saving what we have.", flush=True)

        context.close()

    # Step 3: Save
    full_cleaned = humanize_text(full_cleaned)
    elapsed = time.time() - start
    mins, secs = divmod(int(elapsed), 60)

    print(f"\nStep 3: Saving to {out_path} ...")
    save_text_to_docx(full_cleaned, out_path)
    print(f"  Saved: {out_path}")
    print(f"  {len(full_cleaned.split())} words in cleaned output.")
    print(f"\nDone in {mins}m {secs}s!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

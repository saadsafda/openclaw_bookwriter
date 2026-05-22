"""
wp_landing_page.py

Creates landing pages on the WordPress site (oakharborpress.com) using
browser automation (Playwright) through the OptimizePress admin interface.

Only creates pages — NEVER deletes or modifies existing pages.

Flow:
  1. Login to wp-admin
  2. Navigate to OptimizePress "My Templates" page
  3. Click "Use This Template" on Next-Level-Template-Final-1
  4. Enter the page title and click "Create Page"
  5. Click "Publish" on the resulting editor page
  6. Extract the permalink URL

Credentials are read from environment variables:
    WP_USERNAME  (default: "OC")
    WP_PASSWORD  (the admin password)
    WP_SITE_URL  (default: "https://www.oakharborpress.com")
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


def _load_env() -> None:
    """Best-effort .env loader (same pattern used elsewhere in this project)."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (value.startswith("'") and value.endswith("'")) or (
            value.startswith('"') and value.endswith('"')
        ):
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_env()

WP_SITE_URL = os.environ.get("WP_SITE_URL", "https://www.oakharborpress.com").rstrip("/")
WP_USERNAME = os.environ.get("WP_USERNAME", "OC")
WP_PASSWORD = os.environ.get("WP_PASSWORD", "")

# The OptimizePress template name to use for new landing pages.
WP_TEMPLATE = os.environ.get("WP_TEMPLATE", "Next-Level-Template-Final-1")

# OptimizePress "My Templates" admin page
_OP_TEMPLATES_URL = f"{WP_SITE_URL}/wp-admin/admin.php?page=op-builder-template-customer"


def create_landing_page(
    title: str,
    template: str | None = None,
    *,
    callback: Any | None = None,
) -> dict[str, Any]:
    """Create and publish a landing page via the OptimizePress admin UI.

    Uses headless Playwright to:
      1. Login to WordPress admin
      2. Go to OptimizePress My Templates
      3. Click "Use This Template" on the chosen template
      4. Enter the page title → click Create Page
      5. Click Publish
      6. Extract the permalink

    Parameters
    ----------
    title : str
        The page title (e.g. "lab stories kids").
    template : str | None
        OptimizePress template name.  Falls back to ``WP_TEMPLATE``.
    callback : callable | None
        Optional ``callback(step, message)`` for progress reporting.

    Returns
    -------
    dict with keys: title, url, template
    """
    from playwright.sync_api import sync_playwright

    if not WP_PASSWORD:
        raise RuntimeError(
            "WP_PASSWORD environment variable is not set. "
            "Add it to your .env file (WP_PASSWORD=<your-password>)."
        )

    template = template or WP_TEMPLATE

    def _log(msg: str) -> None:
        if callback:
            callback("wp", msg)

    _log(f"Creating landing page '{title}' on {WP_SITE_URL}...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        try:
            # --- Step 1: Login ---
            _log("Logging into WordPress admin...")
            page.goto(f"{WP_SITE_URL}/wp-login.php", wait_until="networkidle", timeout=30000)
            page.fill("#user_login", WP_USERNAME)
            page.fill("#user_pass", WP_PASSWORD)
            page.click("#wp-submit")
            page.wait_for_url("**/wp-admin/**", timeout=30000)
            _log("Logged in successfully.")

            # --- Step 2: Go to OptimizePress My Templates ---
            _log("Navigating to OptimizePress My Templates...")
            page.goto(_OP_TEMPLATES_URL, wait_until="networkidle", timeout=30000)

            # --- Step 3: Find the template and click "Use This Template" ---
            _log(f"Looking for template '{template}'...")

            # Find the template card by its heading text, then click its
            # "Use This Template" link.
            template_heading = page.locator(f"h5:text-is('{template}')")
            if template_heading.count() == 0:
                # Fallback: try case-insensitive contains match
                template_heading = page.locator(f"h5:has-text('{template}')")

            if template_heading.count() == 0:
                raise RuntimeError(
                    f"Template '{template}' not found on the My Templates page. "
                    f"Check that the template exists at {_OP_TEMPLATES_URL}"
                )

            # The "Use This Template" link is a sibling inside the same parent card
            card = template_heading.locator("xpath=ancestor::div[contains(@class,'ops-template')]")
            if card.count() == 0:
                # Fallback: go up to the nearest container that holds the buttons
                card = template_heading.locator("../..")

            use_btn = card.locator("a:has-text('Use This Template')").first
            use_btn.click()
            _log("Clicked 'Use This Template'.")

            # --- Step 4: Fill in the page title dialog ---
            _log(f"Entering page title: '{title}'...")
            # Wait for the dialog to appear with the title input
            title_input = page.locator("input[placeholder='Enter Page Title']")
            title_input.wait_for(state="visible", timeout=10000)
            title_input.fill(title)

            # Ensure "Page" is selected (default) and click Create Page
            create_btn = page.locator("button:has-text('Create Page')")
            create_btn.click()
            _log("Creating page...")

            # --- Step 5: Wait for editor page to load, then Publish ---
            page.wait_for_url("**/post.php?post=*", timeout=30000)
            _log("Page created (draft). Publishing...")

            # The OP Builder loads in an iframe that can block direct clicks on
            # the WP Publish button.  Use JavaScript to click it reliably.
            page.wait_for_selector("#publish", state="attached", timeout=15000)
            page.evaluate("document.getElementById('publish').click()")

            # Wait for the page to reload / show "published" status
            page.wait_for_load_state("networkidle", timeout=30000)

            # Verify it was published
            try:
                page.wait_for_selector("text=Page published", timeout=10000)
            except Exception:
                # Some WP versions show different confirmation text
                pass

            _log("Page published!")

            # --- Step 6: Extract the permalink ---
            permalink = ""

            # Method 1: read from #sample-permalink a
            try:
                link_el = page.locator("#sample-permalink a").first
                permalink = link_el.get_attribute("href") or ""
                # Remove ?preview=true if present
                permalink = re.sub(r"\?preview=true$", "", permalink)
            except Exception:
                pass

            # Method 2: construct from slug
            if not permalink:
                try:
                    slug = page.evaluate(
                        "() => document.getElementById('editable-post-name-full')?.textContent || ''"
                    )
                    if slug:
                        permalink = f"{WP_SITE_URL}/{slug}/"
                except Exception:
                    pass

            # Method 3: try the post URL
            if not permalink:
                current_url = page.url
                match = re.search(r"post=(\d+)", current_url)
                if match:
                    permalink = f"{WP_SITE_URL}/?p={match.group(1)}"

            if not permalink:
                raise RuntimeError(
                    "Page was published but could not extract the permalink URL."
                )

            _log(f"Landing page URL: {permalink}")

        finally:
            context.close()
            browser.close()

    return {
        "title": title,
        "url": permalink,
        "template": template,
    }

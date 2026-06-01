# OpenClaw — Complete Book Launch Flow

_Last updated: 2026-05-25._

This document walks the end-to-end workflow: writing a book → publishing on Amazon →
launching ads across marketplaces → drafting review-request emails. Read it
top-to-bottom on your first run; later you can jump to any phase by section.

A short legend:

- ✅ = built and live in this codebase
- 🚧 = partially built or planned in the immediate roadmap
- ☁️ = happens outside this app (manual / external service)

### Hard rules (never to be violated)

1. **OpenClaw never publishes books to KDP.** Publishing is always 100% manual, done by you inside the KDP web UI. We do not automate it, attempt it, or call any KDP endpoint.
2. **OpenClaw never connects to your KDP account.** The only Amazon connection we make is to the **Amazon Advertising API** (via an LWA refresh token tied to your ad account). KDP has no public publishing API and we don't try to use any unofficial path.
3. Anywhere this doc mentions "click X in KDP," it means *you* click it in your own KDP browser session — not the app.

---

## What this software does

OpenClaw is two tabs sharing one Flask app at `127.0.0.1:5000`:

| Tab | Path | Purpose |
|---|---|---|
| **Write** | `/` | Generate a book from an outline via OpenClaw agents (images, formatting, listing). |
| **Launch** | `/launch` | Manage Amazon publications, run Sponsored Products campaigns across marketplaces, draft review-request emails. |

A QR code generator lives at `/qr-code`. Auxiliary.

---

## Prerequisites (one-time setup)

1. **Python venv** present at `.venv/`. If not, create one and install deps.
2. **Edit `.env`** at the project root:
   - `LWA_CLIENT_ID`, `LWA_CLIENT_SECRET` — your LWA security profile credentials. One is enough; shared across every company.
   - `LWA_REFRESH_TOKEN` — refresh token for your *first* Amazon Ads account. Auto-seeded as the "Default" company on first DB init.
   - `AMAZON_ADS_ENV` — `production` (recommended) or `sandbox`.
   - `AMAZON_ADS_OAUTH_REDIRECT_URI` — only needed when the OAuth UI lands.
   - `OPENCLAW_*` — OpenClaw agent config used by the Write side.
3. **Start the server**:
   ```bash
   .venv/bin/python app.py
   ```
   Listens on `http://127.0.0.1:5000`.

> The DB (`bookwriter.db`) is auto-migrated on startup. Existing data is preserved across schema upgrades.

---

## Phase 1 — Write the book ✅

**Where**: `/` (Write tab).

1. Upload an outline `.docx` *or* pick one of your previous books from the sidebar.
2. Configure: agent, tone, word counts, image generation toggle/model/quality.
3. Click **Generate**.
4. Watch the live progress: status, current action, logs.
5. When `status` flips to `success`, your book exists as:
   - `final_docx` (clean, single document)
   - `kindle_docx` and `paperback_docx` (format-split copies)
   - generated images (if enabled)
   - a `listing` object (title, subtitle, description, categories) drafted by the `pub-listing-agent`

> A **draft publication is auto-created** on success. You'll find it waiting for you on the Launch tab.

---

## Phase 2 — Edit interior + publish on KDP ☁️ (always manual)

> **You** do this entirely inside the KDP web UI. OpenClaw is not involved and never will be — see Hard Rule #1 at the top of this document.

For each book:

1. Download the **Kindle** and **Paperback** `.docx` files from the book detail screen.
2. Open each in Word / Google Docs, polish formatting, add front-matter, etc.
3. Upload to **KDP** as a new title in each format (Kindle + Paperback).
4. Submit for review. Wait until the book goes **live** in each marketplace.
5. Record each marketplace's **ASIN** and **Amazon product URL** — you'll paste them into the Launch dashboard in Phase 4.
6. **You click "Promote and advertise"** inside your KDP browser session for each marketplace. This is a one-time click *you* make in KDP — OpenClaw does not do this for you. The button is what causes Amazon Ads to issue your *ad account* a profile for that marketplace. Without that profile, the Amazon Advertising API returns "no profile for this marketplace" even when the ASIN is live. (Reminder: we only ever connect to the **Amazon Advertising API** — never to KDP itself. See Hard Rule #2.)

---

## Phase 3 — Configure Amazon Ads companies ✅

OpenClaw supports running ads from **multiple companies' Amazon Ads accounts** (e.g. Company One, Company Two, Company Three). Each company = one LWA refresh token. Profiles within a company are the per-marketplace ad accounts (US / UK / CA / AU…).

1. Open `/launch`.
2. Click **🏢 Accounts** in the header.
3. The modal shows the currently configured companies. "Default" was seeded from your `.env`. Each row shows per-marketplace profile counts (e.g. `US:2 · UK:0 · CA:2 · AU:0`).
4. To add another company, fill the form at the bottom:
   - **Label** — human-readable, e.g. "Royalty Media"
   - **LWA refresh token** — paste a long-lived token issued for that company's ad account
   - **Environment** — usually `production`
   - **Notes** — optional, e.g. "All royalties → Stripe acct_123"
5. Click **Add company**. The server validates the token by listing US profiles. If valid, the new row appears with live profile counts.

**To get a refresh token for a new company**:
```bash
python -m amazon_ads.get_refresh_token
```
…and follow the prompts (browser-based OAuth in CLI form). OAuth-driven UI button is 🚧 next.

**To remove a company**: click the trash icon. Publications linked to it keep their `amazon_account_id` as text but lose the live connection (no orphan delete).

---

## Phase 4 — Set up the publication ✅ (UI partial 🚧)

**Where**: `/launch` → click any publication card.

1. The publication card auto-created in Phase 1 already has the title and listing.
2. Click the card → **Detail modal** opens.
3. Confirm/edit **Title**, **Subtitle**, **Description**, **Categories**.
4. **Per-marketplace ASINs**: today the structured edit UI is wired only for the primary marketplace. Until the per-MP grid lands, use the API directly:
   ```bash
   curl -X POST http://127.0.0.1:5000/api/publications/<pub_id> \
        -H "Content-Type: application/json" \
        -d '{
          "marketplaces": {
            "US": {"asin": "B0XXXX", "url": "https://amazon.com/dp/B0XXXX"},
            "UK": {"asin": "B0YYYY", "url": "https://amazon.co.uk/dp/B0YYYY"},
            "CA": {"asin": "B0ZZZZ", "url": "https://amazon.ca/dp/B0ZZZZ"},
            "AU": {"asin": "B0AAAA", "url": "https://amazon.com.au/dp/B0AAAA"}
          }
        }'
   ```
5. **Pin the publication to a company** (which Amazon Ads account owns this book):
   ```bash
   curl -X POST http://127.0.0.1:5000/api/publications/<pub_id> \
        -H "Content-Type: application/json" \
        -d '{"amazon_account_id":"<account-id-from-accounts-modal>"}'
   ```
   Future ads launches for this publication will default to this company automatically.

---

## Phase 5 — Launch Amazon Ads across marketplaces ✅

**Where**: detail modal → **Amazon Ads** panel → ultimately calls
`POST /api/publications/<pub_id>/amazon-ads/launch`.

### Payload shape (structured, recommended)

```json
{
  "account_id": "default",
  "type": "auto",
  "state": "PAUSED",
  "bidding_strategy": "LEGACY_FOR_SALES",
  "name_template": "{{title}} - {{type}} - {{country}}",
  "dry_run": true,
  "marketplaces": [
    {"country": "US", "default_bid": 1.00, "daily_budget": 5},
    {"country": "UK", "default_bid": 1.20, "daily_budget": 5},
    {"country": "CA", "default_bid": 1.50, "daily_budget": 5},
    {"country": "AU", "default_bid": 0.70, "daily_budget": 5}
  ],
  "keywords":     ["self help","productivity"],
  "category_ids": ["156563011"],
  "target_asins": ["B0AAAAAAAA"]
}
```

Notes:
- `account_id` is optional — falls back to the publication's `amazon_account_id`, then to the first registered account.
- `keywords` is required when `type` = `keyword`; `category_ids` when `type` = `category`; `target_asins` when `type` = `asin`. Ignored otherwise.
- ASIN per marketplace is read from the publication's `marketplaces` dict — you don't pass it again here.
- `name_template` placeholders: `{{title}}`, `{{type}}`, `{{country}}`, `{{ts}}`. Per-row `name_override` wins if present.

### Recommended sequence

1. **Always preview first** with `"dry_run": true`. The response shows each marketplace's resolved profile_id, rendered name, bid, budget, and bidding strategy. Marketplaces missing an ASIN or profile appear as failed rows — fix them before going live.
2. Re-submit with `"dry_run": false` to actually create campaigns. They start **PAUSED** so nothing spends money.
3. Open the Amazon Ads console for each marketplace, sanity-check the campaign, then flip to ENABLED.

### Bidding strategies

| API value | Amazon's UI label |
|---|---|
| `LEGACY_FOR_SALES` | Dynamic bids — down only ← recommended default |
| `AUTO_FOR_SALES` | Dynamic bids — up and down |
| `MANUAL` | Fixed bids |

(Amazon Sponsored Products has no "up only" — that's a Sponsored Brands feature.)

### What gets persisted

Every successful launch writes a row to `amazon_ads_campaigns` with `campaign_id`, `publication_id`, `amazon_account_id`, `bidding_strategy`, marketplace, profile_id, daily_budget, default_bid, plus the raw Amazon API payload.

---

## Phase 6 — Review-request emails (post-purchase) ✅ (drafts only)

**Where**: detail modal → **Review automation** panel.

Goal: nudge readers who bought your book to leave an honest review. Three emails per reader at **+7 / +14 / +30 days** after their trigger date (defaults; configurable).

> ⚠️ **The SMTP auto-send loop is deliberately disabled.** No email is ever sent by this server. Scheduled sends sit at `status: scheduled` until you either (a) mark them as sent manually after pushing through your ESP, or (b) wait for the upcoming "push to MailerLite as draft" feature.

### Flow

1. In the Review panel, set **From name + From email** and confirm the schedule days (default `7, 14, 30`).
2. Three template emails (subject + body, with `{{name}}` / `{{title}}` / `{{review_url}}` / `{{unsubscribe_url}}` placeholders) are seeded. Edit per publication if needed and **Save settings**.
3. Add recipients one by one (or upload a CSV — endpoint exists, UI in progress):
   - Email, name, marketplace, optional notes
   - On add, three **scheduled sends** are created automatically at the configured offsets
4. Click any send to preview the rendered subject + body. Buttons:
   - **Download .eml** — a real RFC 822 message file you can drop into Mail / Thunderbird / etc.
   - **Copy subject / body** — clipboard helpers
   - **Mark as sent** — flips this row's status without actually sending

### Unsubscribe

Every recipient gets a token. The body's `{{unsubscribe_url}}` resolves to `/u/<token>` on this server. Visiting it flips the recipient to `unsubscribed`; future sends auto-cancel.

---

## Phase 7 — Launch promo emails (pre-purchase, MailerLite drafts) 🚧

**Status: not yet ported into this project**. The implementation lives in the older project at `/Users/mac/python_openclaw_bookwriter/` and will be lifted into the new codebase in the next iteration. The intended flow:

1. KDP: set a **3-day free download** window for the Kindle edition.
2. Detail modal → **Launch emails** panel (pending).
3. Choose sequence length (3 / 4 / 5 emails) → click **Generate**. The agent reads the Kindle `.docx` and returns a framework-based draft sequence with day-offsets (`-7, 0, +3, +7` etc).
4. Generate a **3D cover mockup** from the front cover image via `gpt-image-1` (pending endpoint).
5. For each draft, click **Push to MailerLite** → creates a *draft* campaign in MailerLite tagged with the publication + send-date.
6. Open MailerLite, review the draft, press publish. MailerLite handles the actual send.

### Click → review-recipient bridge (🚧 future)

When a free-download clicker engages the email, MailerLite will fire a webhook to this app, creating a `review_recipients` row anchored at the click date — so the existing Phase 6 review automation picks them up at +7 / +14 / +30 days. Closes the loop: free downloader → verified Amazon reviewer.

---

## Roadmap

| Feature | Status |
|---|---|
| Multi-company Amazon Ads accounts | ✅ |
| Multi-marketplace ads launch (one click) with per-MP bids | ✅ |
| Bidding-strategy parameter | ✅ API. UI dropdown 🚧 |
| Campaign name template | ✅ |
| Auto-send disabled — drafts only | ✅ |
| Add Company UI button (manual paste) | ✅ |
| **OAuth Add-Company flow** | 🚧 next |
| **Per-marketplace ASIN editing UI** | 🚧 next |
| **Launch promo emails (MailerLite drafts)** | 🚧 port from old project |
| **3D cover mockup endpoint** | 🚧 |
| **Review automation → MailerLite drafts** | 🚧 |
| **MailerLite click → review-recipient webhook** | 🚧 |

---

## Troubleshooting

| Symptom | Likely cause + fix |
|---|---|
| `"no profile in account 'default' for UK"` on launch | Your refresh token's Amazon Ads account has no profile in that marketplace. Click **Promote and advertise** in KDP for that marketplace and wait a few minutes for the profile to appear. Re-check `/api/amazon-ads/status`. |
| `"OpenClaw CLI timed out"` on `/api/openclaw-models` | Start the OpenClaw gateway: `openclaw gateway`. Models endpoint depends on it. |
| MailerLite settings empty | The MailerLite client + settings endpoints are scheduled for Phase 7's port. Settings table exists but isn't populated yet. |
| `"LWA_REFRESH_TOKEN is not set"` on a non-default account | The DB row is missing `lwa_refresh_token`. Re-add the company via the Accounts modal. |
| Template / HTML edits don't appear after editing | Flask runs with `debug=False` and Jinja caches templates. Restart the server, or add `app.config["TEMPLATES_AUTO_RELOAD"] = True` for dev. |
| Sandbox profiles are empty | Sandbox doesn't pre-provision profiles. Use `amazon_ads.profiles.create_test_account(marketplace="UK", ...)` once per marketplace to register one, or switch `AMAZON_ADS_ENV` to `production` (the `testAccounts` endpoint lives on the prod host). |

---

## API cheat-sheet

### Amazon Ads

| Method | Path | Notes |
|---|---|---|
| GET | `/api/amazon-ads/status` | Per-account profile counts |
| GET | `/api/amazon-ads/accounts` | List companies (token stripped) |
| POST | `/api/amazon-ads/accounts` | Add company (`label`, `lwa_refresh_token`, `env`, `notes`); validates against `/v2/profiles` |
| DELETE | `/api/amazon-ads/accounts/<id>` | Remove company |
| GET | `/api/amazon-ads/accounts/<id>/profiles` | Per-account profiles for all 4 marketplaces |
| GET | `/api/amazon-ads/categories/search?q=&profile_id=` | Look up targetable category IDs |
| POST | `/api/publications/<id>/amazon-ads/launch` | Multi-marketplace launch (see Phase 5) |

### Publications

| Method | Path | Notes |
|---|---|---|
| GET | `/api/publications` | List |
| GET | `/api/publications/<id>` | Detail (includes campaign history) |
| POST | `/api/publications` | Create from book or upload-zip |
| POST | `/api/publications/<id>` | Patch any field (title, marketplaces, amazon_account_id…) |
| DELETE | `/api/publications/<id>` | Remove |
| GET | `/api/publications/<id>/file/<kindle\|paperback\|cover>` | Download |

### Review automation

| Method | Path | Notes |
|---|---|---|
| GET | `/api/publications/<id>/review/settings` | Templates + schedule + SMTP availability flag |
| POST | `/api/publications/<id>/review/settings` | Save |
| GET | `/api/publications/<id>/review/recipients` | List |
| POST | `/api/publications/<id>/review/recipients` | Add recipient (auto-schedules 3 sends) |
| DELETE | `/api/review/recipients/<id>` | Remove |
| GET | `/api/publications/<id>/review/sends` | List scheduled / sent rows |
| GET | `/api/review/sends/<id>/preview` | Render subject + body |
| GET | `/api/review/sends/<id>/eml` | Download as `.eml` |
| POST | `/api/review/sends/<id>/mark-sent` | Manual mark-as-sent |
| POST | `/api/review/sends/<id>/cancel` | Cancel |
| GET / POST | `/u/<token>` | Recipient self-service unsubscribe |

### Books / jobs (Write side)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/books` | List |
| GET | `/api/books/<id>` | Detail |
| POST | `/api/jobs` | Start a new write job |
| GET | `/api/jobs/<id>/status` | Poll status + logs |
| GET | `/api/jobs/<id>/listing` | The generated KDP listing |
| GET | `/api/jobs/<id>/download/<kind>` | input / output / final / kindle / paperback |

---

## One-line summary

> Write a book on `/` → polish + publish on KDP manually → register the company on `/launch` once → set ASINs + pin to company per publication → dry-run multi-marketplace ads → flip dry_run off → add review recipients → push to MailerLite as drafts (pending) → review + publish from inside MailerLite.

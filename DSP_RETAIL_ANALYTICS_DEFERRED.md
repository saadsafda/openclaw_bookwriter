# DSP & Retail Analytics — Deferred

_Status: not built. Deferred on 2026-06-10 because it requires a separate Amazon
API access grant that is not yet in place._

## Why it's not built yet

Everything currently in the Analytics dashboard (Overview, Keywords, Search
Terms, Trends, Bid Optimizer, Negative Keywords) runs on the **Sponsored
Products** APIs, which the existing LWA app + refresh tokens already authorize.

DSP and full Retail Analytics live behind **different, approval-gated APIs**:

| Capability | API | Access requirement |
|---|---|---|
| DSP campaign/line-item reporting | Amazon DSP API | Separate DSP API allow-listing; typically agency / managed-DSP accounts only |
| Near-real-time event stream | Amazon Marketing Stream | Separate onboarding + an AWS account to receive the Firehose/SQS stream |
| Share-of-voice / competitor / market share | Amazon Marketing Cloud (AMC) / Brand Analytics | Brand registry + AMC instance, or DSP entitlement |

Without those grants the endpoints would authenticate fine but return empty /
403 responses — so there's nothing useful to show until access is in place.

## What to do when access is granted

1. **Confirm entitlement** — in the Amazon Ads console, verify the account has
   DSP API access and note the **DSP advertiser/profile IDs** (these differ from
   the Sponsored Products `profileId`).
2. **Marketing Stream (optional, for real-time)** — stand up an AWS account with
   a Firehose/SQS destination and run the Marketing Stream subscription flow.
3. **Build steps (mirrors the SP analytics we already have):**
   - `amazon_ads/dsp_reporting.py` — create → poll → download DSP reports
     (the lifecycle is the same shape as `reporting.py`, different host/paths).
   - New cache tables in `db.py` (`dsp_*_cache`) following the
     `analytics_*_cache` pattern.
   - New endpoints in `analytics_routes.py`: `/api/analytics/dsp/*`.
   - New tabs in `templates/analytics.html`: "DSP" and "Market Share".
4. **Auth note** — DSP may need a wider LWA scope than
   `advertising::campaign_management`. Re-run the OAuth connect flow
   (Settings → "Grant Access to Amazon Ads") after the scope is added so the
   refresh token carries the new permission.

## What IS built and working now

- ✅ Sponsored Products analytics (spend/sales/ACoS/ROAS) — campaign, keyword,
  search-term, daily-trend
- ✅ Bid Optimizer with **one-click apply** and an **auto-optimize toggle**
  (applies suggestions automatically, skips any change > 50% as a safety rail)
- ✅ Negative-keyword automation — flags zero-sale, click-burning search terms
  and adds them as NEGATIVE_EXACT with one click or "Add all as negative"

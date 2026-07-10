"""Amazon Ads async reporting — fetches SP campaign spend/sales for ACoS.

Flow:
    create report → poll until COMPLETED → download gzip JSON → enrich rows
    with _spend / _salesNd / _acosN / _roasN computed fields (N = 7, 14, 30).

The caller passes profile_id + marketplace so we pick the right regional host.
account_id is optional (None → falls back to the .env default token).
"""

from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import time

import requests

from .client import AmazonAdsClient


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _date_str(days_ago: int) -> str:
    return (dt.date.today() - dt.timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _today() -> str:
    return dt.date.today().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Report lifecycle
# ---------------------------------------------------------------------------


def _create_report_with_payload(client: AmazonAdsClient, payload: dict) -> str:
    """POST /reporting/reports with an arbitrary payload → return reportId."""
    resp = client.post("/reporting/reports", json=payload)
    if resp.status_code not in (200, 202):
        raise RuntimeError(
            f"Report creation failed ({resp.status_code}): {resp.text[:400]}"
        )
    return resp.json()["reportId"]


def _create_report(client: AmazonAdsClient) -> str:
    """POST /reporting/reports → return reportId.

    Always fetches a 30-day window with all three attribution windows (7d/14d/30d)
    so the frontend can switch windows without a new API call.
    """
    payload = {
        "name": "OpenClaw ACoS",
        "startDate": _date_str(30),
        "endDate": _today(),
        "configuration": {
            "adProduct": "SPONSORED_PRODUCTS",
            "groupBy": ["campaign"],
            "columns": [
                "campaignId",
                "campaignName",
                "campaignStatus",
                "impressions",
                "clicks",
                "cost",
                "purchases7d",
                "sales7d",
                "purchases14d",
                "sales14d",
                "purchases30d",
                "sales30d",
            ],
            "reportTypeId": "spCampaigns",
            "timeUnit": "SUMMARY",
            "format": "GZIP_JSON",
        },
    }
    return _create_report_with_payload(client, payload)


def _poll_report(client: AmazonAdsClient, report_id: str, *, timeout: int = 540) -> str:
    """Poll GET /reporting/reports/{reportId} until COMPLETED → return download URL."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/reporting/reports/{report_id}")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Poll failed ({resp.status_code}): {resp.text[:200]}"
            )
        data = resp.json()
        status = (data.get("status") or "").upper()
        if status == "COMPLETED":
            return data["url"]
        if status in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"Report ended with status {status}: {data.get('statusDetails','')}")
        time.sleep(8)
    raise TimeoutError(f"Report {report_id} not ready after {timeout}s")


def _download(url: str) -> list[dict]:
    """Download presigned S3 URL (no auth headers). Handles gzip or plain JSON."""
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    try:
        with gzip.open(io.BytesIO(resp.content)) as f:
            return json.loads(f.read())
    except Exception:
        return json.loads(resp.content)


# ---------------------------------------------------------------------------
# Enrichment — compute _spend / _salesN / _acosN / _roasN
# ---------------------------------------------------------------------------


def _enrich(rows: list[dict]) -> list[dict]:
    """Add computed ACoS / ROAS fields for windows 7 / 14 / 30 days."""
    for row in rows:
        spend = float(row.get("cost") or 0)
        row["_spend"] = round(spend, 2)
        for w in (7, 14, 30):
            sales = float(row.get(f"sales{w}d") or 0)
            purchases = int(row.get(f"purchases{w}d") or 0)
            row[f"_sales{w}"] = round(sales, 2)
            row[f"_purchases{w}"] = purchases
            row[f"_acos{w}"] = (
                round(spend / sales * 100, 1) if sales > 0 else None
            )
            row[f"_roas{w}"] = (
                round(sales / spend, 2) if spend > 0 else None
            )
    return rows


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def fetch_campaign_acos(
    profile_id: str | int,
    *,
    marketplace: str,
    account_id: str | None = None,
) -> list[dict]:
    """Full flow: create → poll → download → enrich.

    Returns one dict per Sponsored Products campaign with all raw Amazon
    fields plus the computed _spend / _salesN / _acosN / _roasN helpers.
    """
    client = AmazonAdsClient(
        marketplace=marketplace,
        profile_id=str(profile_id),
        account_id=account_id,
    )
    report_id = _create_report(client)
    url = _poll_report(client, report_id)
    rows = _download(url)
    return _enrich(rows)


def fetch_keyword_performance(
    profile_id: str | int,
    *,
    marketplace: str,
    account_id: str | None = None,
) -> list[dict]:
    """SP targeting report grouped by keyword/target for keyword-level analytics."""
    client = AmazonAdsClient(
        marketplace=marketplace,
        profile_id=str(profile_id),
        account_id=account_id,
    )
    payload = {
        "name": "OpenClaw Keywords",
        "startDate": _date_str(30),
        "endDate": _today(),
        "configuration": {
            "adProduct": "SPONSORED_PRODUCTS",
            "groupBy": ["targeting"],
            "columns": [
                "campaignId",
                "campaignName",
                "adGroupId",
                "adGroupName",
                "keywordId",
                "keyword",
                "keywordType",
                "matchType",
                "targeting",
                "keywordBid",
                "impressions",
                "clicks",
                "cost",
                "purchases7d",
                "sales7d",
                "purchases14d",
                "sales14d",
                "purchases30d",
                "sales30d",
            ],
            "reportTypeId": "spTargeting",
            "timeUnit": "SUMMARY",
            "format": "GZIP_JSON",
        },
    }
    report_id = _create_report_with_payload(client, payload)
    url = _poll_report(client, report_id)
    rows = _download(url)
    return _enrich(rows)


def fetch_daily_trends(
    profile_id: str | int,
    *,
    marketplace: str,
    account_id: str | None = None,
) -> list[dict]:
    """SP campaigns report with DAILY time unit — 30 days of daily rows for trend charts."""
    client = AmazonAdsClient(
        marketplace=marketplace,
        profile_id=str(profile_id),
        account_id=account_id,
    )
    payload = {
        "name": "OpenClaw Trends",
        "startDate": _date_str(30),
        "endDate": _today(),
        "configuration": {
            "adProduct": "SPONSORED_PRODUCTS",
            "groupBy": ["campaign"],
            "columns": [
                "date",
                "campaignId",
                "campaignName",
                "impressions",
                "clicks",
                "cost",
                "purchases7d",
                "sales7d",
                "purchases14d",
                "sales14d",
                "purchases30d",
                "sales30d",
            ],
            "reportTypeId": "spCampaigns",
            "timeUnit": "DAILY",
            "format": "GZIP_JSON",
        },
    }
    report_id = _create_report_with_payload(client, payload)
    url = _poll_report(client, report_id)
    rows = _download(url)
    # Aggregate across campaigns per date
    by_date: dict[str, dict] = {}
    for row in rows:
        d = row.get("date") or ""
        if not d:
            continue
        if d not in by_date:
            by_date[d] = {"date": d, "spend": 0.0, "sales7d": 0.0, "sales14d": 0.0, "sales30d": 0.0, "clicks": 0, "impressions": 0}
        by_date[d]["spend"] = round(by_date[d]["spend"] + float(row.get("cost") or 0), 2)
        by_date[d]["sales7d"] = round(by_date[d]["sales7d"] + float(row.get("sales7d") or 0), 2)
        by_date[d]["sales14d"] = round(by_date[d]["sales14d"] + float(row.get("sales14d") or 0), 2)
        by_date[d]["sales30d"] = round(by_date[d]["sales30d"] + float(row.get("sales30d") or 0), 2)
        by_date[d]["clicks"] += int(row.get("clicks") or 0)
        by_date[d]["impressions"] += int(row.get("impressions") or 0)
    return sorted(by_date.values(), key=lambda r: r["date"])


def fetch_search_terms(
    profile_id: str | int,
    *,
    marketplace: str,
    account_id: str | None = None,
) -> list[dict]:
    """SP search term report — which customer queries triggered ads and how they performed."""
    client = AmazonAdsClient(
        marketplace=marketplace,
        profile_id=str(profile_id),
        account_id=account_id,
    )
    payload = {
        "name": "OpenClaw Search Terms",
        "startDate": _date_str(30),
        "endDate": _today(),
        "configuration": {
            "adProduct": "SPONSORED_PRODUCTS",
            "groupBy": ["searchTerm"],
            "columns": [
                "campaignId",
                "campaignName",
                "adGroupId",
                "adGroupName",
                "keywordId",
                "keyword",
                "matchType",
                "searchTerm",
                "impressions",
                "clicks",
                "cost",
                "purchases7d",
                "sales7d",
                "purchases14d",
                "sales14d",
                "purchases30d",
                "sales30d",
            ],
            "reportTypeId": "spSearchTerm",
            "timeUnit": "SUMMARY",
            "format": "GZIP_JSON",
        },
    }
    report_id = _create_report_with_payload(client, payload)
    url = _poll_report(client, report_id)
    rows = _download(url)
    return _enrich(rows)


def compute_bid_suggestions(
    keyword_rows: list[dict],
    *,
    target_acos: float = 30.0,
    window: int = 14,
) -> list[dict]:
    """Rule-based bid optimization suggestions from keyword performance data.

    Rules:
    - ACoS > target×1.5 AND clicks ≥ 5  → reduce bid 20%
    - clicks ≥ 10 AND 0 sales           → reduce bid 30% (wasted spend)
    - ACoS < target×0.7 AND impr ≥ 100  → increase bid 15% (room to scale)
    """
    suggestions = []
    for row in keyword_rows:
        kw_text = (
            row.get("keyword")
            or row.get("targeting")
            or "Unknown"
        )
        match_type = row.get("matchType") or "AUTO"
        campaign = row.get("campaignName") or ""
        spend = float(row.get("_spend") or 0)
        sales = float(row.get(f"_sales{window}") or 0)
        acos = row.get(f"_acos{window}")
        clicks = int(row.get("clicks") or 0)
        impressions = int(row.get("impressions") or 0)

        if spend < 0.01:
            continue  # no activity — nothing to optimize

        # Identifiers needed to push a bid change back to Amazon. These are
        # tagged onto each row by the analytics layer (_collect_data) plus the
        # keywordId that comes straight from the targeting report.
        entity_id = str(row.get("keywordId") or "")
        mt_upper = (match_type or "").upper()
        is_keyword = mt_upper in ("EXACT", "PHRASE", "BROAD")
        # keywordBid comes straight from the report — pass it through so the
        # apply flow can skip the extra lookup and the UI can show the real bid.
        try:
            cur_bid = float(row.get("keywordBid")) if row.get("keywordBid") not in (None, "") else None
        except (TypeError, ValueError):
            cur_bid = None

        def _make(action, severity, reason, pct, *, sales_val=sales, acos_val=acos):
            return {
                "keyword": kw_text,
                "match_type": match_type,
                "campaign": campaign,
                "spend": spend,
                "sales": sales_val,
                "clicks": clicks,
                "impressions": impressions,
                "acos": acos_val,
                "action": action,
                "severity": severity,
                "reason": reason,
                "bid_change_pct": pct,
                # --- fields used by the one-click apply flow ---
                "entity_id": entity_id,
                "is_keyword": is_keyword,
                "can_apply": bool(entity_id),
                "current_bid": cur_bid,
                "profile_id": str(row.get("_profile_id") or ""),
                "account_id": row.get("_account_id") or "",
                "marketplace": row.get("_marketplace") or "",
            }

        if acos is None and clicks >= 10:
            suggestions.append(_make(
                "reduce_bid", "high",
                f"{clicks} clicks, 0 sales — wasted spend", -30,
                sales_val=0.0, acos_val=None,
            ))
        elif acos is not None and acos > target_acos * 1.5 and clicks >= 5:
            suggestions.append(_make(
                "reduce_bid", "medium",
                f"ACoS {acos}% is {round(acos / target_acos, 1)}× target ({target_acos}%)", -20,
            ))
        elif acos is not None and acos < target_acos * 0.7 and impressions >= 100:
            suggestions.append(_make(
                "increase_bid", "opportunity",
                f"ACoS {acos}% well below target — room to scale", +15,
            ))

    # Sort: high severity first, then by spend descending
    order = {"high": 0, "medium": 1, "opportunity": 2}
    suggestions.sort(key=lambda s: (order.get(s["severity"], 3), -s["spend"]))
    return suggestions


def compute_negative_suggestions(
    search_term_rows: list[dict],
    *,
    target_acos: float = 30.0,
    window: int = 14,
    min_clicks: int = 8,
    min_spend: float = 0.50,
) -> list[dict]:
    """Flag wasteful customer search terms to add as NEGATIVE_EXACT keywords.

    A term is "wasteful" when it spent real money and converted nothing:

        clicks ≥ min_clicks  AND  0 sales  AND  spend ≥ min_spend

    Adding it as a negative exact stops ads showing for that exact query.
    We only target zero-sale terms — a high-but-nonzero ACoS term still makes
    *some* sales, so killing it outright would be too aggressive.

    Each suggestion carries the campaignId / adGroupId it belongs to (negatives
    are ad-group scoped) plus the profile/account/marketplace to write to.
    """
    suggestions = []
    seen: set[tuple] = set()

    for row in search_term_rows:
        term = (row.get("searchTerm") or "").strip()
        if not term:
            continue
        spend = float(row.get("_spend") or 0)
        sales = float(row.get(f"_sales{window}") or 0)
        clicks = int(row.get("clicks") or 0)
        impressions = int(row.get("impressions") or 0)
        campaign_id = str(row.get("campaignId") or "")
        ad_group_id = str(row.get("adGroupId") or "")

        # Only zero-sale, click-burning, real-spend terms.
        if sales > 0 or clicks < min_clicks or spend < min_spend:
            continue

        # One negative per (ad group, term) — the report can repeat a term
        # across match types within the same ad group.
        key = (campaign_id, ad_group_id, term.lower())
        if key in seen:
            continue
        seen.add(key)

        can_apply = bool(campaign_id and ad_group_id)
        suggestions.append({
            "search_term": term,
            "matched_keyword": row.get("keyword") or "—",
            "match_type": row.get("matchType") or "",
            "campaign": row.get("campaignName") or "",
            "spend": spend,
            "clicks": clicks,
            "impressions": impressions,
            "reason": f"{clicks} clicks, {fmt_money(spend)} spent, 0 sales",
            # --- fields for the one-click apply flow ---
            "negative_match_type": "NEGATIVE_EXACT",
            "campaign_id": campaign_id,
            "ad_group_id": ad_group_id,
            "can_apply": can_apply,
            "profile_id": str(row.get("_profile_id") or ""),
            "account_id": row.get("_account_id") or "",
            "marketplace": row.get("_marketplace") or "",
        })

    # Worst offenders (most wasted spend) first.
    suggestions.sort(key=lambda s: -s["spend"])
    return suggestions


def fmt_money(v: float) -> str:
    """Small helper for human-readable spend in suggestion reasons."""
    return f"${v:,.2f}"

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
    resp = client.post("/reporting/reports", json=payload)
    if resp.status_code not in (200, 202):
        raise RuntimeError(
            f"Report creation failed ({resp.status_code}): {resp.text[:400]}"
        )
    return resp.json()["reportId"]


def _poll_report(client: AmazonAdsClient, report_id: str, *, timeout: int = 240) -> str:
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

"""Add negative keywords to existing Sponsored Products ad groups (v3).

The Search Term report surfaces customer queries that triggered your ads.
Some of them burn clicks with zero sales — pure waste. This module adds those
queries as NEGATIVE_EXACT keywords so your ads stop showing for them.

Negative keywords are scoped to an ad group, so each suggestion carries the
campaignId + adGroupId it came from.
"""

from __future__ import annotations

from typing import Any

from .client import AmazonAdsClient

# Vendor content type for SP v3 negative keywords (matches campaigns.py).
_CT_NEG_KEYWORD = "application/vnd.spNegativeKeyword.v3+json"


def add_negative_keyword(
    profile_id: str | int,
    *,
    marketplace: str,
    account_id: str | None = None,
    campaign_id: str,
    ad_group_id: str,
    keyword_text: str,
    match_type: str = "NEGATIVE_EXACT",
) -> str:
    """Add one negative keyword to an ad group. Returns the new id.

    ``match_type`` is NEGATIVE_EXACT or NEGATIVE_PHRASE.
    """
    if not campaign_id or not ad_group_id or not keyword_text:
        raise RuntimeError("campaign_id, ad_group_id and keyword_text are all required.")

    mt = (match_type or "NEGATIVE_EXACT").upper()
    if mt not in ("NEGATIVE_EXACT", "NEGATIVE_PHRASE"):
        mt = "NEGATIVE_EXACT"

    client = AmazonAdsClient(
        marketplace=marketplace,
        profile_id=str(profile_id),
        account_id=account_id,
    )
    payload = {
        "negativeKeywords": [
            {
                "campaignId": str(campaign_id),
                "adGroupId": str(ad_group_id),
                "state": "ENABLED",
                "keywordText": keyword_text,
                "matchType": mt,
            }
        ]
    }
    resp = client.post(
        "/sp/negativeKeywords",
        json=payload,
        content_type=_CT_NEG_KEYWORD,
        accept=_CT_NEG_KEYWORD,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"Add negative keyword failed ({resp.status_code}): {resp.text[:300]}")

    block = resp.json().get("negativeKeywords") or {}
    errors = block.get("error") or []
    if errors:
        msgs = []
        for e in errors:
            detail = e.get("errors") or e.get("message") or e
            msgs.append(str(detail))
        # Amazon rejects duplicates — surface a friendly message for that case.
        joined = "; ".join(msgs)[:300]
        if "duplicate" in joined.lower():
            raise RuntimeError("Already a negative keyword in this ad group.")
        raise RuntimeError(f"Amazon rejected the negative keyword: {joined}")

    success = block.get("success") or []
    if not success:
        raise RuntimeError(f"Amazon returned no success: {resp.text[:200]}")
    return str(success[0].get("negativeKeywordId") or "")

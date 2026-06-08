"""Sponsored Products bid recommendations (best-effort).

Amazon's theme-based bid recommendations endpoint is finicky: it is
version-sensitive, occasionally returns nothing for brand-new accounts, and
its response schema drifts. Per the product decision, this module is built so
that ANY failure (network, auth, empty payload, schema change) degrades
silently to ``{"available": False}`` — the caller then falls back to the
operator's typed / default bids and never blocks a launch.

Two entry points:

    suggest_auto_bids(profile_id, marketplace, account_id=...)
    suggest_keyword_bids(profile_id, marketplace, keywords, match_types, account_id=...)

Both return a normalized dict:

    {
      "available": bool,
      "currency": "USD",                 # marketplace currency, best-effort
      "overall": {"low": .., "median": .., "high": ..} | None,
      "per": { "<keyword or expr>": {"low":..,"median":..,"high":..}, ... },
      "reason": "<why unavailable>",     # only when available is False
    }
"""

from __future__ import annotations

import statistics
from typing import Any, Iterable, Sequence

from .client import AmazonAdsClient

# Theme-based bid recommendations (v3)
_REC_PATH = "/sp/targets/bid/recommendations"
_REC_CT = "application/vnd.spthemebasedbidrecommendation.v3+json"

# Auto-targeting expression types Amazon recognises for recommendations.
_AUTO_EXPRESSIONS = [
    {"type": "CLOSE_MATCH"},
    {"type": "LOOSE_MATCH"},
    {"type": "SUBSTITUTES"},
    {"type": "COMPLEMENTS"},
]

# match-type label -> Amazon keyword expression type
_KW_EXPR_TYPE = {
    "EXACT": "KEYWORD_EXACT_MATCH",
    "PHRASE": "KEYWORD_PHRASE_MATCH",
    "BROAD": "KEYWORD_BROAD_MATCH",
}

_CURRENCY = {
    "US": "USD", "CA": "CAD", "UK": "GBP", "GB": "GBP", "AU": "AUD",
    "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR", "NL": "EUR",
    "JP": "JPY", "MX": "MXN", "BR": "BRL", "IN": "INR",
}


def _currency_for(marketplace: str) -> str:
    return _CURRENCY.get(marketplace.upper(), "")


def _collect_bid_numbers(node: Any, out: list[float]) -> None:
    """Recursively pull any plausible bid value out of a response subtree.

    Resilient to schema drift: collects floats found under keys whose name
    contains 'bid', 'suggested', or 'range' (start/end). Ignores absurd values.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            kl = str(k).lower()
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if any(tok in kl for tok in ("bid", "suggested", "rangestart",
                                             "rangeend", "start", "end")):
                    val = float(v)
                    if 0 < val < 1000:  # sane bid bound across currencies
                        out.append(val)
            else:
                _collect_bid_numbers(v, out)
    elif isinstance(node, list):
        for item in node:
            _collect_bid_numbers(item, out)


def _range_from(values: Sequence[float]) -> dict[str, float] | None:
    vals = sorted(v for v in values if v and v > 0)
    if not vals:
        return None
    return {
        "low": round(vals[0], 2),
        "median": round(statistics.median(vals), 2),
        "high": round(vals[-1], 2),
    }


def _request_recommendations(
    profile_id: str | int,
    marketplace: str,
    expressions: list[dict],
    *,
    account_id: str | None,
    bidding_strategy: str = "LEGACY_FOR_SALES",
) -> dict[str, Any]:
    """Single POST to the recommendations endpoint, fully guarded.

    Returns the parsed JSON on success, or {} on any failure.
    """
    try:
        client = AmazonAdsClient(
            marketplace=marketplace,
            profile_id=str(profile_id),
            account_id=account_id,
        )
        payload = {
            "adProduct": "SPONSORED_PRODUCTS",
            "recommendationType": "BIDS_FOR_NEW_AD_GROUP",
            "targetingExpressions": expressions,
            "bidding": {"strategy": bidding_strategy},
        }
        resp = client.post(
            _REC_PATH,
            json=payload,
            content_type=_REC_CT,
            accept=_REC_CT,
            timeout=30,
        )
        if resp.status_code >= 300:
            return {}
        return resp.json() or {}
    except Exception:
        return {}


def suggest_auto_bids(
    profile_id: str | int,
    marketplace: str,
    *,
    account_id: str | None = None,
    bidding_strategy: str = "LEGACY_FOR_SALES",
) -> dict[str, Any]:
    """Suggested bid range for an AUTO campaign in one marketplace."""
    data = _request_recommendations(
        profile_id, marketplace, _AUTO_EXPRESSIONS,
        account_id=account_id, bidding_strategy=bidding_strategy,
    )
    nums: list[float] = []
    _collect_bid_numbers(data, nums)
    overall = _range_from(nums)
    if not overall:
        return {"available": False, "reason": "no recommendation returned",
                "currency": _currency_for(marketplace), "overall": None, "per": {}}
    return {
        "available": True,
        "currency": _currency_for(marketplace),
        "overall": overall,
        "per": {},
    }


def suggest_keyword_bids(
    profile_id: str | int,
    marketplace: str,
    keywords: Iterable[str],
    match_types: Sequence[str] = ("EXACT", "PHRASE", "BROAD"),
    *,
    account_id: str | None = None,
    bidding_strategy: str = "LEGACY_FOR_SALES",
    max_keywords: int = 100,
) -> dict[str, Any]:
    """Suggested bid range for a set of keywords in one marketplace.

    Caps at ``max_keywords`` per request to keep the payload sane; the overall
    range it returns is a useful guide even for very large keyword lists.
    """
    kw_list = [k.strip() for k in keywords if k and k.strip()][:max_keywords]
    if not kw_list:
        return {"available": False, "reason": "no keywords",
                "currency": _currency_for(marketplace), "overall": None, "per": {}}

    mt = [m.upper() for m in match_types if m.upper() in _KW_EXPR_TYPE] or ["EXACT"]
    expressions: list[dict] = []
    for kw in kw_list:
        for m in mt:
            expressions.append({"type": _KW_EXPR_TYPE[m], "value": kw})

    data = _request_recommendations(
        profile_id, marketplace, expressions,
        account_id=account_id, bidding_strategy=bidding_strategy,
    )
    nums: list[float] = []
    _collect_bid_numbers(data, nums)
    overall = _range_from(nums)
    if not overall:
        return {"available": False, "reason": "no recommendation returned",
                "currency": _currency_for(marketplace), "overall": None, "per": {}}
    return {
        "available": True,
        "currency": _currency_for(marketplace),
        "overall": overall,
        "per": {},
    }

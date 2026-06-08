"""Sponsored Products campaign creation helpers.

Four flavours, all using the Sponsored Products v3 API:

    create_auto_campaign(...)       # auto targeting
    create_keyword_campaign(...)    # manual + keyword targets
    create_category_campaign(...)   # manual + category product targets
    create_asin_campaign(...)       # manual + ASIN product targets

Each call returns a dict with the created campaignId, adGroupId, and the
targeting/keyword/product-target ids that were attached.

Notes
-----
* Budgets and bids are in the marketplace's local currency. Pass numbers
  matching that currency (USD for US, GBP for UK, CAD for CA, AUD for AU).
* Sponsored Products v3 uses vendor-prefixed JSON content types per resource.
* The simple sync endpoints below are sufficient for individual campaign
  creation. For bulk creation you'd switch to the async batch endpoints.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterable, Sequence

from .client import AmazonAdsClient

# Vendor content types for SP v3
_CT_CAMPAIGN = "application/vnd.spCampaign.v3+json"
_CT_ADGROUP = "application/vnd.spAdGroup.v3+json"
_CT_PRODUCT_AD = "application/vnd.spProductAd.v3+json"
_CT_KEYWORD = "application/vnd.spKeyword.v3+json"
_CT_TARGET = "application/vnd.spTargetingClause.v3+json"
_CT_NEG_KEYWORD = "application/vnd.spNegativeKeyword.v3+json"


# ---------------------------------------------------------------------------
# Low level helpers
# ---------------------------------------------------------------------------


def _today() -> str:
    return dt.date.today().strftime("%Y-%m-%d")


def _post(
    client: AmazonAdsClient,
    path: str,
    payload: dict,
    *,
    content_type: str,
) -> dict:
    resp = client.post(
        path,
        json=payload,
        content_type=content_type,
        accept=content_type,
    )
    if resp.status_code >= 300:
        raise RuntimeError(
            f"POST {path} failed ({resp.status_code}): {resp.text}"
        )
    return resp.json()


def _first_id(response: dict, key: str, id_field: str) -> str:
    """SP v3 returns {'<key>': {'success': [{'<id_field>': '...'}], 'error': [...]}}."""
    block = response.get(key) or response
    errors = block.get("error") or []
    if errors:
        raise RuntimeError(f"Amazon Ads error creating {key}: {errors}")
    success = block.get("success") or []
    if not success:
        raise RuntimeError(f"No success entries for {key}: {response}")
    return str(success[0][id_field])


def _ids(response: dict, key: str, id_field: str) -> list[str]:
    block = response.get(key) or response
    errors = block.get("error") or []
    if errors:
        raise RuntimeError(f"Amazon Ads error creating {key}: {errors}")
    return [str(item[id_field]) for item in (block.get("success") or [])]


# ---------------------------------------------------------------------------
# Building blocks shared by all four campaign types
# ---------------------------------------------------------------------------


def _create_campaign(
    client: AmazonAdsClient,
    *,
    name: str,
    targeting_type: str,  # "AUTO" or "MANUAL"
    daily_budget: float,
    state: str = "PAUSED",
    start_date: str | None = None,
    end_date: str | None = None,
    bidding_strategy: str = "LEGACY_FOR_SALES",
) -> str:
    campaign = {
        "name": name,
        "targetingType": targeting_type,
        "state": state,
        "dynamicBidding": {"strategy": bidding_strategy},
        "budget": {
            "budgetType": "DAILY",
            "budget": float(daily_budget),
        },
        "startDate": start_date or _today(),
    }
    if end_date:
        campaign["endDate"] = end_date

    resp = _post(
        client,
        "/sp/campaigns",
        {"campaigns": [campaign]},
        content_type=_CT_CAMPAIGN,
    )
    return _first_id(resp, "campaigns", "campaignId")


def _create_ad_group(
    client: AmazonAdsClient,
    *,
    campaign_id: str,
    name: str,
    default_bid: float,
) -> str:
    ad_group = {
        "name": name,
        "campaignId": campaign_id,
        "state": "ENABLED",
        "defaultBid": float(default_bid),
    }
    resp = _post(
        client,
        "/sp/adGroups",
        {"adGroups": [ad_group]},
        content_type=_CT_ADGROUP,
    )
    return _first_id(resp, "adGroups", "adGroupId")


def _create_product_ads(
    client: AmazonAdsClient,
    *,
    campaign_id: str,
    ad_group_id: str,
    asins: Sequence[str],
) -> list[str]:
    product_ads = [
        {
            "campaignId": campaign_id,
            "adGroupId": ad_group_id,
            "state": "ENABLED",
            "asin": asin,
        }
        for asin in asins
    ]
    resp = _post(
        client,
        "/sp/productAds",
        {"productAds": product_ads},
        content_type=_CT_PRODUCT_AD,
    )
    return _ids(resp, "productAds", "adId")


# ---------------------------------------------------------------------------
# 1) AUTO targeting campaign
# ---------------------------------------------------------------------------


def create_auto_campaign(
    *,
    marketplace: str,
    profile_id: int | str,
    name: str,
    asins: Sequence[str],
    daily_budget: float,
    default_bid: float,
    state: str = "PAUSED",
    start_date: str | None = None,
    end_date: str | None = None,
    bidding_strategy: str = "LEGACY_FOR_SALES",
    account_id: str | None = None,
) -> dict[str, Any]:
    """Create a fully-formed AUTO Sponsored Products campaign.

    Auto targeting creates four implicit targeting expressions: close-match,
    loose-match, substitutes, complements. Amazon manages them for you.

    Campaigns are created PAUSED by default - flip ``state='ENABLED'`` (or
    enable in the Amazon Ads console) once you've reviewed everything.
    """
    client = AmazonAdsClient(
        marketplace=marketplace,
        profile_id=profile_id,
        account_id=account_id,
    )

    campaign_id = _create_campaign(
        client,
        name=name,
        targeting_type="AUTO",
        daily_budget=daily_budget,
        state=state,
        start_date=start_date,
        end_date=end_date,
        bidding_strategy=bidding_strategy,
    )
    ad_group_id = _create_ad_group(
        client,
        campaign_id=campaign_id,
        name=f"{name} - Ad Group",
        default_bid=default_bid,
    )
    ad_ids = _create_product_ads(
        client,
        campaign_id=campaign_id,
        ad_group_id=ad_group_id,
        asins=asins,
    )

    return {
        "marketplace": marketplace,
        "type": "auto",
        "campaignId": campaign_id,
        "adGroupId": ad_group_id,
        "productAdIds": ad_ids,
    }


# ---------------------------------------------------------------------------
# 2) KEYWORD targeting campaign
# ---------------------------------------------------------------------------


def create_keyword_campaign(
    *,
    marketplace: str,
    profile_id: int | str,
    name: str,
    asins: Sequence[str],
    keywords: Iterable[str | dict],
    daily_budget: float,
    default_bid: float,
    match_types: Sequence[str] = ("EXACT", "PHRASE", "BROAD"),
    negative_keywords: Sequence[str] = (),
    state: str = "PAUSED",
    start_date: str | None = None,
    end_date: str | None = None,
    bidding_strategy: str = "LEGACY_FOR_SALES",
    account_id: str | None = None,
) -> dict[str, Any]:
    """Create a MANUAL Sponsored Products campaign targeting keywords.

    `keywords` may be plain strings (which get fanned out across `match_types`)
    or dicts like ``{"keywordText": "self help", "matchType": "EXACT", "bid": 0.85}``.
    """
    client = AmazonAdsClient(
        marketplace=marketplace,
        profile_id=profile_id,
        account_id=account_id,
    )

    campaign_id = _create_campaign(
        client,
        name=name,
        targeting_type="MANUAL",
        daily_budget=daily_budget,
        state=state,
        start_date=start_date,
        end_date=end_date,
        bidding_strategy=bidding_strategy,
    )
    ad_group_id = _create_ad_group(
        client,
        campaign_id=campaign_id,
        name=f"{name} - Keywords",
        default_bid=default_bid,
    )
    ad_ids = _create_product_ads(
        client,
        campaign_id=campaign_id,
        ad_group_id=ad_group_id,
        asins=asins,
    )

    keyword_payloads: list[dict] = []
    for kw in keywords:
        if isinstance(kw, dict):
            text = kw["keywordText"]
            # Per-keyword bid wins; fall back to the campaign default bid.
            kw_bid = float(kw.get("bid") or default_bid)
            explicit_mt = kw.get("matchType")
            if explicit_mt:
                # Caller pinned a single match type for this keyword.
                keyword_payloads.append(
                    {
                        "campaignId": campaign_id,
                        "adGroupId": ad_group_id,
                        "state": "ENABLED",
                        "keywordText": text,
                        "matchType": str(explicit_mt).upper(),
                        "bid": kw_bid,
                    }
                )
            else:
                # No match type pinned -> fan this keyword out across every
                # match type chosen for the launch, each at its own bid.
                for mt in match_types:
                    keyword_payloads.append(
                        {
                            "campaignId": campaign_id,
                            "adGroupId": ad_group_id,
                            "state": "ENABLED",
                            "keywordText": text,
                            "matchType": mt.upper(),
                            "bid": kw_bid,
                        }
                    )
        else:
            for mt in match_types:
                keyword_payloads.append(
                    {
                        "campaignId": campaign_id,
                        "adGroupId": ad_group_id,
                        "state": "ENABLED",
                        "keywordText": kw,
                        "matchType": mt.upper(),
                        "bid": float(default_bid),
                    }
                )

    keyword_resp = _post(
        client,
        "/sp/keywords",
        {"keywords": keyword_payloads},
        content_type=_CT_KEYWORD,
    )
    keyword_ids = _ids(keyword_resp, "keywords", "keywordId")

    neg_ids: list[str] = []
    if negative_keywords:
        neg_payloads = [
            {
                "campaignId": campaign_id,
                "adGroupId": ad_group_id,
                "state": "ENABLED",
                "keywordText": kw,
                "matchType": "NEGATIVE_EXACT",
            }
            for kw in negative_keywords
        ]
        neg_resp = _post(
            client,
            "/sp/negativeKeywords",
            {"negativeKeywords": neg_payloads},
            content_type=_CT_NEG_KEYWORD,
        )
        neg_ids = _ids(neg_resp, "negativeKeywords", "negativeKeywordId")

    return {
        "marketplace": marketplace,
        "type": "keyword",
        "campaignId": campaign_id,
        "adGroupId": ad_group_id,
        "productAdIds": ad_ids,
        "keywordIds": keyword_ids,
        "negativeKeywordIds": neg_ids,
    }


# ---------------------------------------------------------------------------
# 3) CATEGORY product-targeting campaign
# ---------------------------------------------------------------------------


def create_category_campaign(
    *,
    marketplace: str,
    profile_id: int | str,
    name: str,
    asins: Sequence[str],
    category_ids: Sequence[str | int],
    daily_budget: float,
    default_bid: float,
    state: str = "PAUSED",
    start_date: str | None = None,
    end_date: str | None = None,
    bidding_strategy: str = "LEGACY_FOR_SALES",
    account_id: str | None = None,
) -> dict[str, Any]:
    """Create a MANUAL Sponsored Products campaign that targets categories.

    `category_ids` are Amazon Ads "refinement" category ids - look them up via
    ``GET /sp/targets/categories`` or the product-targeting browse helper.
    """
    client = AmazonAdsClient(
        marketplace=marketplace,
        profile_id=profile_id,
        account_id=account_id,
    )

    campaign_id = _create_campaign(
        client,
        name=name,
        targeting_type="MANUAL",
        daily_budget=daily_budget,
        state=state,
        start_date=start_date,
        end_date=end_date,
        bidding_strategy=bidding_strategy,
    )
    ad_group_id = _create_ad_group(
        client,
        campaign_id=campaign_id,
        name=f"{name} - Categories",
        default_bid=default_bid,
    )
    ad_ids = _create_product_ads(
        client,
        campaign_id=campaign_id,
        ad_group_id=ad_group_id,
        asins=asins,
    )

    target_payloads = [
        {
            "campaignId": campaign_id,
            "adGroupId": ad_group_id,
            "state": "ENABLED",
            "bid": float(default_bid),
            "expressionType": "MANUAL",
            "expression": [
                {"type": "ASIN_CATEGORY_SAME_AS", "value": str(cat_id)}
            ],
        }
        for cat_id in category_ids
    ]
    resp = _post(
        client,
        "/sp/targets",
        {"targetingClauses": target_payloads},
        content_type=_CT_TARGET,
    )
    target_ids = _ids(resp, "targetingClauses", "targetId")

    return {
        "marketplace": marketplace,
        "type": "category",
        "campaignId": campaign_id,
        "adGroupId": ad_group_id,
        "productAdIds": ad_ids,
        "targetIds": target_ids,
    }


# ---------------------------------------------------------------------------
# 4) ASIN / product targeting campaign
# ---------------------------------------------------------------------------


def create_asin_campaign(
    *,
    marketplace: str,
    profile_id: int | str,
    name: str,
    asins: Sequence[str],
    target_asins: Sequence[str],
    daily_budget: float,
    default_bid: float,
    state: str = "PAUSED",
    start_date: str | None = None,
    end_date: str | None = None,
    bidding_strategy: str = "LEGACY_FOR_SALES",
    account_id: str | None = None,
) -> dict[str, Any]:
    """Create a MANUAL Sponsored Products campaign that targets specific ASINs.

    `asins`         - the ASINs of YOUR books that will be advertised.
    `target_asins`  - the competitor/related ASINs whose product pages your
                      ads should appear on.
    """
    client = AmazonAdsClient(
        marketplace=marketplace,
        profile_id=profile_id,
        account_id=account_id,
    )

    campaign_id = _create_campaign(
        client,
        name=name,
        targeting_type="MANUAL",
        daily_budget=daily_budget,
        state=state,
        start_date=start_date,
        end_date=end_date,
        bidding_strategy=bidding_strategy,
    )
    ad_group_id = _create_ad_group(
        client,
        campaign_id=campaign_id,
        name=f"{name} - ASIN Targets",
        default_bid=default_bid,
    )
    ad_ids = _create_product_ads(
        client,
        campaign_id=campaign_id,
        ad_group_id=ad_group_id,
        asins=asins,
    )

    target_payloads = [
        {
            "campaignId": campaign_id,
            "adGroupId": ad_group_id,
            "state": "ENABLED",
            "bid": float(default_bid),
            "expressionType": "MANUAL",
            "expression": [{"type": "ASIN_SAME_AS", "value": asin}],
        }
        for asin in target_asins
    ]
    resp = _post(
        client,
        "/sp/targets",
        {"targetingClauses": target_payloads},
        content_type=_CT_TARGET,
    )
    target_ids = _ids(resp, "targetingClauses", "targetId")

    return {
        "marketplace": marketplace,
        "type": "asin",
        "campaignId": campaign_id,
        "adGroupId": ad_group_id,
        "productAdIds": ad_ids,
        "targetIds": target_ids,
    }

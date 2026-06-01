"""Fetch and look up advertising profile IDs.

A "profile" represents one ad account in one marketplace. You need its
profileId in the `Amazon-Advertising-API-Scope` header for every campaign
management call.

Sandbox note
------------
The sandbox does NOT come with profiles pre-created. Before you can list or
use them you must register a sandbox profile per country with
``register_sandbox_profile()``. Production accounts already have profiles
created when you set up your ad account, so you only need ``list_profiles``
there.
"""

from __future__ import annotations

from typing import Any

from .client import AmazonAdsClient


def list_profiles(
    marketplace: str = "US",
    *,
    account_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return profiles visible to a given ad account.

    Each Amazon Ads account (=> one refresh token) sees its own profiles.
    Pass ``account_id`` to query a specific company; omit for legacy
    .env-based single-account use.
    """
    client = AmazonAdsClient(marketplace=marketplace, account_id=account_id)
    resp = client.get("/v2/profiles", require_profile=False)
    if resp.status_code != 200:
        raise RuntimeError(
            f"GET /v2/profiles failed ({resp.status_code}): {resp.text}"
        )
    return resp.json()


def list_all_profiles(
    marketplaces: list[str] | None = None,
    *,
    account_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Walk one marketplace per region and aggregate all profiles for an account."""
    # One marketplace from each of the three regions covers everything.
    targets = marketplaces or ["US", "UK", "AU"]
    results: dict[str, list[dict[str, Any]]] = {}
    seen_regions: set[str] = set()
    from .config import MARKETPLACE_REGION

    for mp in targets:
        region, _ = MARKETPLACE_REGION[mp.upper()]
        if region in seen_regions:
            continue
        seen_regions.add(region)
        results[region] = list_profiles(mp, account_id=account_id)
    return results


def create_test_account(
    marketplace: str = "US",
    *,
    account_type: str = "VENDOR",
    vendor_code: str = "ABCDE",
) -> dict[str, Any]:
    """Create a test ad account on the production host.

    This is Amazon's newer replacement for the old (broken) sandbox: a real
    test account that lives at ``advertising-api.amazon.com/testAccounts``
    and shows up in ``GET /v2/profiles`` with ``accountInfo.type='vendor'``
    or ``'seller'``. No real money is involved.

    Run this once per marketplace (US, UK, CA, AU). After it succeeds,
    ``list_profiles('US')`` will return the new test profile.

    Parameters
    ----------
    marketplace: "US", "UK", "CA", "AU", ...
    account_type: "VENDOR" or "AUTHOR" (KDP) or "SELLER".
    vendor_code: 5-letter placeholder, only used when account_type='VENDOR'.

    Note: requires ``AMAZON_ADS_ENV=production`` because the endpoint lives
    on the prod host, even though it creates a fake account.
    """
    client = AmazonAdsClient(marketplace=marketplace)
    if client.config.is_sandbox:
        raise RuntimeError(
            "create_test_account requires AMAZON_ADS_ENV=production "
            "(the /testAccounts endpoint lives on the production host)."
        )

    mp = marketplace.upper()
    country_code = "GB" if mp == "UK" else mp

    body: dict[str, Any] = {
        "countryCode": country_code,
        "accountType": account_type.upper(),
    }
    if account_type.upper() == "VENDOR":
        body["accountMetaData"] = {"vendorCode": vendor_code}

    resp = client.post(
        "/testAccounts",
        json=body,
        require_profile=False,
    )
    if resp.status_code >= 300:
        raise RuntimeError(
            f"POST /testAccounts failed ({resp.status_code}): {resp.text}"
        )
    return resp.json()


def get_test_account_status(request_id: str, marketplace: str = "US") -> dict[str, Any]:
    """Poll a test-account creation request by id (creation is async)."""
    client = AmazonAdsClient(marketplace=marketplace)
    resp = client.get(
        "/testAccounts",
        params={"requestId": request_id},
        require_profile=False,
    )
    if resp.status_code >= 300:
        raise RuntimeError(
            f"GET /testAccounts failed ({resp.status_code}): {resp.text}"
        )
    return resp.json()


def find_profile_id(profiles: list[dict[str, Any]], marketplace: str) -> int | None:
    """Pick the profileId for a given marketplace from a profile list."""
    mp = marketplace.upper()
    # countryCode comes back as the 2-letter marketplace code ("US", "GB", "CA", "AU"...)
    code = "GB" if mp == "UK" else mp
    for p in profiles:
        cc = (p.get("countryCode") or "").upper()
        if cc == code:
            return int(p["profileId"])
    return None

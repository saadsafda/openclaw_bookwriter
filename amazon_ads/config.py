"""Amazon Ads API endpoint configuration and env loading."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    # dotenv is optional; env vars may already be set
    pass


# ---------------------------------------------------------------------------
# Marketplace -> region mapping
# ---------------------------------------------------------------------------
# Amazon Ads splits the API into three regions. One refresh token works across
# all of them, but every request must hit the right host and carry the right
# profileId for the marketplace.

REGION_NA = "NA"
REGION_EU = "EU"
REGION_FE = "FE"  # Far East

# marketplace code -> (region, country name)
MARKETPLACE_REGION = {
    "US": (REGION_NA, "United States"),
    "CA": (REGION_NA, "Canada"),
    "MX": (REGION_NA, "Mexico"),
    "BR": (REGION_NA, "Brazil"),
    "UK": (REGION_EU, "United Kingdom"),
    "GB": (REGION_EU, "United Kingdom"),
    "DE": (REGION_EU, "Germany"),
    "FR": (REGION_EU, "France"),
    "IT": (REGION_EU, "Italy"),
    "ES": (REGION_EU, "Spain"),
    "NL": (REGION_EU, "Netherlands"),
    "SE": (REGION_EU, "Sweden"),
    "PL": (REGION_EU, "Poland"),
    "AE": (REGION_EU, "United Arab Emirates"),
    "TR": (REGION_EU, "Turkey"),
    "EG": (REGION_EU, "Egypt"),
    "SA": (REGION_EU, "Saudi Arabia"),
    "IN": (REGION_EU, "India"),
    "JP": (REGION_FE, "Japan"),
    "AU": (REGION_FE, "Australia"),
    "SG": (REGION_FE, "Singapore"),
}

# Production hosts per region
PROD_HOSTS = {
    REGION_NA: "https://advertising-api.amazon.com",
    REGION_EU: "https://advertising-api-eu.amazon.com",
    REGION_FE: "https://advertising-api-fe.amazon.com",
}

# Sandbox hosts per region
SANDBOX_HOSTS = {
    REGION_NA: "https://advertising-api-test.amazon.com",
    REGION_EU: "https://advertising-api-test-eu.amazon.com",
    REGION_FE: "https://advertising-api-test-fe.amazon.com",
}

# LWA token endpoint is global
LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
LWA_AUTHORIZE_URL = "https://www.amazon.com/ap/oa"

# Scope required for the Advertising API
LWA_SCOPE = "advertising::campaign_management"


@dataclass(frozen=True)
class AmazonAdsConfig:
    client_id: str
    client_secret: str
    refresh_token: str | None
    env: str  # "sandbox" or "production"
    redirect_uri: str

    @property
    def is_sandbox(self) -> bool:
        return self.env.lower() == "sandbox"

    def host_for(self, marketplace: str) -> str:
        region, _ = MARKETPLACE_REGION[marketplace.upper()]
        hosts = SANDBOX_HOSTS if self.is_sandbox else PROD_HOSTS
        return hosts[region]


def load_config(account_id: str | None = None) -> AmazonAdsConfig:
    """Load Amazon Ads config for one account.

    Per-account credentials take priority over .env globals, so each account
    can use its own LWA Developer App.  Fall-back chain:

        client_id     : account DB row → LWA_CLIENT_ID env var
        client_secret : account DB row → LWA_CLIENT_SECRET env var
        refresh_token : account DB row → LWA_REFRESH_TOKEN env var (legacy)
        env           : account DB row → AMAZON_ADS_ENV env var
    """
    # Global defaults from .env
    global_client_id = os.getenv("LWA_CLIENT_ID", "").strip()
    global_client_secret = os.getenv("LWA_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv(
        "AMAZON_ADS_OAUTH_REDIRECT_URI", "http://localhost:8080/callback"
    ).strip()
    env_fallback = os.getenv("AMAZON_ADS_ENV", "sandbox").strip().lower()

    refresh_token: str | None = None
    env = env_fallback
    client_id = global_client_id
    client_secret = global_client_secret

    if account_id:
        # Lazy import to avoid a hard module-load cycle with db.py.
        try:
            import db as _bookdb  # type: ignore
            acct = _bookdb.get_amazon_ads_account(account_id, include_token=True)
        except Exception:
            acct = None
        if acct is None:
            raise RuntimeError(
                f"Amazon Ads account '{account_id}' not found in database. "
                "Add it via Settings → Amazon Ads first."
            )
        refresh_token = (acct.get("lwa_refresh_token") or "").strip() or None
        env = (acct.get("env") or env_fallback).strip().lower() or env_fallback
        # Per-account credentials override the global .env values
        per_cid = (acct.get("client_id") or "").strip()
        per_secret = (acct.get("lwa_client_secret") or "").strip()
        if per_cid:
            client_id = per_cid
        if per_secret:
            client_secret = per_secret
    else:
        refresh_token = os.getenv("LWA_REFRESH_TOKEN", "").strip() or None

    if not client_id or not client_secret:
        raise RuntimeError(
            "LWA_CLIENT_ID and LWA_CLIENT_SECRET are not set. "
            "Either add them to your .env file (shared default) or set them "
            "per-account in Settings → Amazon Ads."
        )

    return AmazonAdsConfig(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        env=env,
        redirect_uri=redirect_uri,
    )

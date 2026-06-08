"""ACoS reporting routes.

Two endpoints, both scoped to a publication:

    GET  /api/publications/<pub_id>/acos?window=14
        Returns cached ACoS data. If the cache is stale (>30 min) or missing,
        triggers a background fetch and returns whatever is currently cached
        with status="fetching". The frontend polls until status="ready".

    POST /api/publications/<pub_id>/acos/refresh
        Force-resets the cache for every profile used by this publication's
        campaigns and triggers fresh background fetches. Returns immediately.

Cache is per Amazon Ads profile_id (one row per profile). A publication can
span multiple profiles (one per marketplace), so both endpoints aggregate
across all profiles that have campaigns for this publication.
"""

from __future__ import annotations

import json
import threading
import time

from flask import jsonify, request

import db

CACHE_TTL = 1800  # seconds — treat cache as stale after 30 minutes

# Track which profile_ids are currently being fetched so we never double-fetch.
_active_fetches: set[str] = set()
_active_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Background fetch
# ---------------------------------------------------------------------------


def _do_fetch(profile_id: str, *, account_id: str, marketplace: str) -> None:
    """Background worker: run the full Amazon reporting flow for one profile."""
    with _active_lock:
        if profile_id in _active_fetches:
            return  # already in flight
        _active_fetches.add(profile_id)
    try:
        from amazon_ads.reporting import fetch_campaign_acos  # lazy — avoids circular import at startup
        db.set_acos_cache_status(profile_id, "fetching")
        rows = fetch_campaign_acos(
            profile_id,
            marketplace=marketplace,
            account_id=account_id or None,
        )
        db.set_acos_cache(
            profile_id,
            account_id=account_id,
            marketplace=marketplace,
            campaigns=rows,
            status="ready",
        )
    except Exception as exc:
        db.set_acos_cache_status(profile_id, "error", error=str(exc)[:500])
    finally:
        with _active_lock:
            _active_fetches.discard(profile_id)


def _trigger_fetch(profile_id: str, *, account_id: str, marketplace: str) -> None:
    t = threading.Thread(
        target=_do_fetch,
        args=(profile_id,),
        kwargs={"account_id": account_id, "marketplace": marketplace},
        daemon=True,
    )
    t.start()


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _profiles_for_pub(pub_id: str) -> dict[str, dict]:
    """Return {profile_id: {marketplace, account_id, campaign_ids}} for a publication."""
    campaigns = db.list_amazon_campaigns(publication_id=pub_id) or []
    profiles: dict[str, dict] = {}
    for c in campaigns:
        pid = str(c["profile_id"])
        if pid not in profiles:
            profiles[pid] = {
                "marketplace": c["marketplace"],
                "account_id": c.get("amazon_account_id", ""),
                "campaign_ids": set(),
            }
        profiles[pid]["campaign_ids"].add(c["campaign_id"])
    return profiles


def _build_response(pub_id: str, window: int) -> dict:
    """Build the full JSON response for GET /api/publications/<pub_id>/acos."""
    campaigns_db = db.list_amazon_campaigns(publication_id=pub_id) or []
    profiles = _profiles_for_pub(pub_id)

    if not profiles:
        return {
            "status": "no_campaigns",
            "window_days": window,
            "fetched_at": None,
            "campaigns": [],
            "summary": {
                "total_spend": 0,
                "total_sales": 0,
                "total_purchases": 0,
                "acos": None,
                "roas": None,
            },
        }

    # For each profile: check cache freshness, trigger background fetch if stale
    any_fetching = False
    any_error = False
    oldest_fetched_at = None
    acos_by_campaign: dict[str, dict] = {}

    for pid, info in profiles.items():
        cache = db.get_acos_cache(pid)

        stale = (
            not cache
            or cache["status"] == "idle"
            or (
                cache["status"] == "ready"
                and time.time() - cache["fetched_at"] > CACHE_TTL
            )
        )

        if stale and (not cache or cache.get("status") != "fetching"):
            _trigger_fetch(
                pid,
                account_id=info["account_id"],
                marketplace=info["marketplace"],
            )

        if not cache:
            any_fetching = True
            continue

        s = cache["status"]
        if s == "fetching":
            any_fetching = True
        if s == "error":
            any_error = True

        fat = cache.get("fetched_at") or 0
        oldest_fetched_at = fat if oldest_fetched_at is None else min(oldest_fetched_at, fat)

        try:
            rows = json.loads(cache.get("campaigns_json") or "[]")
        except Exception:
            rows = []

        # Index rows by Amazon campaignId, restrict to this pub's campaigns
        for row in rows:
            cid = str(row.get("campaignId") or "")
            if cid and cid in info["campaign_ids"]:
                acos_by_campaign[cid] = row

    # Enrich our DB campaign records
    enriched = []
    for c in campaigns_db:
        ec = dict(c)
        row = acos_by_campaign.get(c["campaign_id"])
        if row:
            ec["ads_spend"] = row.get("_spend")
            ec["ads_sales"] = row.get(f"_sales{window}")
            ec["ads_acos"] = row.get(f"_acos{window}")
            ec["ads_roas"] = row.get(f"_roas{window}")
            ec["ads_impressions"] = row.get("impressions")
            ec["ads_clicks"] = row.get("clicks")
        enriched.append(ec)

    # Publication-level totals
    total_spend = sum(float(r.get("_spend") or 0) for r in acos_by_campaign.values())
    total_sales = sum(
        float(r.get(f"_sales{window}") or 0) for r in acos_by_campaign.values()
    )
    total_purchases = sum(
        int(r.get(f"_purchases{window}") or 0) for r in acos_by_campaign.values()
    )
    acos = round(total_spend / total_sales * 100, 1) if total_sales > 0 else None
    roas = round(total_sales / total_spend, 2) if total_spend > 0 else None

    overall = "fetching" if any_fetching else ("error" if any_error else "ready")

    return {
        "status": overall,
        "window_days": window,
        "fetched_at": oldest_fetched_at,
        "campaigns": enriched,
        "summary": {
            "total_spend": round(total_spend, 2),
            "total_sales": round(total_sales, 2),
            "total_purchases": total_purchases,
            "acos": acos,
            "roas": roas,
        },
    }


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def _country_to_marketplace(cc: str) -> str:
    """Map Amazon countryCode (e.g. 'GB') to our marketplace code (e.g. 'UK')."""
    return "UK" if cc.upper() == "GB" else cc.upper()


def _build_dashboard_response(window: int) -> dict:
    """Aggregate ACoS for the dashboard.

    Source of truth is the Amazon Ads reporting cache — not just our local DB.
    That means campaigns created directly on Amazon console (not through OpenClaw)
    will also appear once the cache has been populated via Pull data.
    """
    # --- Collect known profiles from two sources ---
    # 1) Our local campaign DB (campaigns we launched through the app)
    local_campaigns = db.list_amazon_campaigns() or []
    local_by_cid: dict[str, dict] = {c["campaign_id"]: c for c in local_campaigns}

    profiles: dict[str, dict] = {}
    for c in local_campaigns:
        pid = str(c["profile_id"])
        if pid not in profiles:
            profiles[pid] = {
                "marketplace": c["marketplace"],
                "account_id": c.get("amazon_account_id", ""),
            }

    # 2) Previously fetched profiles from acos_cache (includes profiles we
    #    discovered via Pull data even without a local campaign)
    for cp in db.list_acos_cache_profiles():
        pid = cp["profile_id"]
        if pid not in profiles and cp.get("marketplace"):
            profiles[pid] = {
                "marketplace": cp["marketplace"],
                "account_id": cp.get("account_id", ""),
            }

    any_fetching = False
    any_error = False
    oldest_fetched_at = None

    # All campaigns from Amazon reporting data (superset of local DB)
    all_api_rows: list[dict] = []

    for pid, info in profiles.items():
        cache = db.get_acos_cache(pid)
        stale = (
            not cache
            or cache["status"] == "idle"
            or (cache["status"] == "ready" and time.time() - cache["fetched_at"] > CACHE_TTL)
        )
        if stale and (not cache or cache.get("status") != "fetching"):
            _trigger_fetch(pid, account_id=info["account_id"], marketplace=info["marketplace"])

        if not cache:
            any_fetching = True
            continue
        if cache["status"] == "fetching":
            any_fetching = True
        if cache["status"] == "error":
            any_error = True

        fat = cache.get("fetched_at") or 0
        oldest_fetched_at = fat if oldest_fetched_at is None else min(oldest_fetched_at, fat)

        try:
            rows = json.loads(cache.get("campaigns_json") or "[]")
        except Exception:
            rows = []

        for row in rows:
            cid = str(row.get("campaignId") or "")
            if not cid:
                continue
            # Merge Amazon reporting row with local campaign metadata (if any)
            local = local_by_cid.get(cid, {})
            ec: dict = {
                "campaign_id": cid,
                "name": row.get("campaignName") or local.get("name", ""),
                "marketplace": info["marketplace"],
                "campaign_type": local.get("campaign_type") or row.get("targetingType", "SP"),
                "state": (row.get("campaignStatus") or local.get("state", "PAUSED")).upper(),
                "daily_budget": local.get("daily_budget"),
                "publication_id": local.get("publication_id", ""),
                "book_id": local.get("book_id", ""),
                "profile_id": pid,
                "in_openclaw": bool(local),
                # ACoS fields
                "ads_spend": row.get("_spend"),
                "ads_sales": row.get(f"_sales{window}"),
                "ads_acos": row.get(f"_acos{window}"),
                "ads_roas": row.get(f"_roas{window}"),
                "ads_impressions": row.get("impressions"),
                "ads_clicks": row.get("clicks"),
            }
            all_api_rows.append(ec)

    # If nothing came from the API yet, fall back to local DB campaign skeletons
    if not all_api_rows and local_campaigns:
        for c in local_campaigns:
            all_api_rows.append({**dict(c), "in_openclaw": True})

    # --- Aggregations ---
    mp_agg: dict[str, dict] = {}
    pub_agg: dict[str, dict] = {}
    total_spend = total_sales = total_orders = 0.0

    for c in all_api_rows:
        sp = float(c.get("ads_spend") or 0)
        sa = float(c.get("ads_sales") or 0)
        total_spend += sp
        total_sales += sa

        mp = c["marketplace"]
        if mp not in mp_agg:
            mp_agg[mp] = {"spend": 0.0, "sales": 0.0}
        mp_agg[mp]["spend"] += sp
        mp_agg[mp]["sales"] += sa

        pid_pub = c.get("publication_id") or ""
        if pid_pub:
            if pid_pub not in pub_agg:
                pub_agg[pid_pub] = {"spend": 0.0, "sales": 0.0}
            pub_agg[pid_pub]["spend"] += sp
            pub_agg[pid_pub]["sales"] += sa

    # Total orders from the raw cache rows (can't recompute from enriched without raw)
    for cp in db.list_acos_cache_profiles():
        try:
            rows = json.loads(cp.get("campaigns_json") or "[]")
        except Exception:
            rows = []
        for row in rows:
            total_orders += int(row.get(f"purchases{window}d") or 0)

    for pid_pub, agg in pub_agg.items():
        agg["acos"] = round(agg["spend"] / agg["sales"] * 100, 1) if agg["sales"] > 0 else None
        agg["roas"] = round(agg["sales"] / agg["spend"], 2) if agg["spend"] > 0 else None

    acos = round(total_spend / total_sales * 100, 1) if total_sales > 0 else None
    roas = round(total_sales / total_spend, 2) if total_spend > 0 else None

    overall = "fetching" if any_fetching else ("error" if any_error else "ready")
    if not profiles:
        overall = "no_campaigns"

    return {
        "status": overall,
        "window_days": window,
        "fetched_at": oldest_fetched_at,
        "campaigns": all_api_rows,
        "by_marketplace": {
            mp: {
                "spend": round(v["spend"], 2),
                "sales": round(v["sales"], 2),
                "acos": round(v["spend"] / v["sales"] * 100, 1) if v["sales"] > 0 else None,
            }
            for mp, v in mp_agg.items()
        },
        "by_publication": {
            pid_pub: {
                "spend": round(v["spend"], 2),
                "sales": round(v["sales"], 2),
                "acos": v["acos"],
                "roas": v["roas"],
            }
            for pid_pub, v in pub_agg.items()
        },
        "summary": {
            "total_spend": round(total_spend, 2),
            "total_sales": round(total_sales, 2),
            "total_orders": int(total_orders),
            "acos": acos,
            "roas": roas,
        },
    }


def register(app) -> None:

    @app.get("/api/acos/dashboard")
    def get_dashboard_acos():
        window = int(request.args.get("window", 14))
        if window not in (7, 14, 30):
            return jsonify({"error": "window must be 7, 14, or 30"}), 400
        return jsonify(_build_dashboard_response(window))

    @app.post("/api/acos/dashboard/refresh")
    def refresh_dashboard_acos():
        """Force-refresh ACoS for all profiles.

        Discovers profiles from three sources (in priority order):
        1. All configured Amazon Ads accounts via the Profiles API
        2. Previously cached profiles (acos_cache table)
        3. Local campaign records

        This means clicking Pull data will find campaigns created directly on
        Amazon console, not only ones launched through OpenClaw.
        """
        seen: dict[str, dict] = {}  # profile_id → {marketplace, account_id}

        # --- Source 1: auto-discover from every configured Amazon account ---
        try:
            from amazon_ads.profiles import list_all_profiles
            accounts = db.list_amazon_ads_accounts() or []
            for acct in accounts:
                account_id = acct["id"]
                try:
                    profiles_by_region = list_all_profiles(account_id=account_id)
                    for _region, profile_list in profiles_by_region.items():
                        for prof in profile_list:
                            pid = str(prof.get("profileId") or "")
                            cc = prof.get("countryCode") or ""
                            mp = _country_to_marketplace(cc) if cc else ""
                            if pid and mp:
                                seen[pid] = {"marketplace": mp, "account_id": account_id}
                                db.seed_acos_profile(pid, mp, account_id)
                except Exception:
                    pass  # account token invalid / not configured — skip silently
        except Exception:
            pass

        # --- Source 2: previously cached profiles ---
        for cp in db.list_acos_cache_profiles():
            pid = cp["profile_id"]
            if pid not in seen and cp.get("marketplace"):
                seen[pid] = {
                    "marketplace": cp["marketplace"],
                    "account_id": cp.get("account_id", ""),
                }

        # --- Source 3: local campaign records ---
        for c in (db.list_amazon_campaigns() or []):
            pid = str(c["profile_id"])
            if pid not in seen:
                seen[pid] = {
                    "marketplace": c["marketplace"],
                    "account_id": c.get("amazon_account_id", ""),
                }

        # Trigger fresh fetch for every discovered profile
        for pid, info in seen.items():
            db.set_acos_cache_status(pid, "idle")
            _trigger_fetch(pid, account_id=info["account_id"], marketplace=info["marketplace"])

        return jsonify({"ok": True, "status": "fetching", "profiles": list(seen.keys())})

    @app.get("/api/publications/<pub_id>/acos")
    def get_pub_acos(pub_id):
        window = int(request.args.get("window", 14))
        if window not in (7, 14, 30):
            return jsonify({"error": "window must be 7, 14, or 30"}), 400
        return jsonify(_build_response(pub_id, window))

    @app.post("/api/publications/<pub_id>/acos/refresh")
    def refresh_pub_acos(pub_id):
        body = request.get_json(silent=True) or {}
        profiles = _profiles_for_pub(pub_id)

        refreshed: list[str] = []
        for pid, info in profiles.items():
            db.set_acos_cache_status(pid, "idle")
            _trigger_fetch(
                pid,
                account_id=info["account_id"],
                marketplace=info["marketplace"],
            )
            refreshed.append(pid)

        return jsonify({"ok": True, "status": "fetching", "profiles": refreshed})

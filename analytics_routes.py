"""Analytics routes — Intentwise-style Amazon Ads analytics dashboard.

Endpoints:
    GET  /analytics                         Serve the analytics page
    GET  /api/analytics/overview            Summary + campaign data (from acos_cache)
    GET  /api/analytics/keywords            Keyword-level performance
    GET  /api/analytics/trends              Daily spend/sales trend
    GET  /api/analytics/search-terms        Search term report
    GET  /api/analytics/bid-suggestions     Rule-based bid optimization
    POST /api/analytics/refresh             Trigger fresh fetch for all data types
"""

from __future__ import annotations

import json
import threading
import time

from flask import jsonify, render_template, request

import db

CACHE_TTL = 3600  # 1 hour — keyword/trend data changes slowly

_active_fetches: dict[str, set[str]] = {
    "keywords": set(),
    "trends": set(),
    "searchterms": set(),
}
_active_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Background fetch workers
# ---------------------------------------------------------------------------

def _do_fetch(kind: str, profile_id: str, *, account_id: str, marketplace: str) -> None:
    with _active_lock:
        if profile_id in _active_fetches[kind]:
            return
        _active_fetches[kind].add(profile_id)
    try:
        from amazon_ads.reporting import (
            fetch_keyword_performance,
            fetch_daily_trends,
            fetch_search_terms,
        )
        db.set_analytics_cache_status(profile_id, kind, "fetching")
        if kind == "keywords":
            rows = fetch_keyword_performance(profile_id, marketplace=marketplace, account_id=account_id or None)
        elif kind == "trends":
            rows = fetch_daily_trends(profile_id, marketplace=marketplace, account_id=account_id or None)
        else:
            rows = fetch_search_terms(profile_id, marketplace=marketplace, account_id=account_id or None)
        db.set_analytics_cache(
            profile_id, kind,
            account_id=account_id,
            marketplace=marketplace,
            rows=rows,
            status="ready",
        )
    except Exception as exc:
        db.set_analytics_cache_status(profile_id, kind, "error", error=str(exc)[:500])
    finally:
        with _active_lock:
            _active_fetches[kind].discard(profile_id)


def _trigger_fetch(kind: str, profile_id: str, *, account_id: str, marketplace: str) -> None:
    t = threading.Thread(
        target=_do_fetch,
        args=(kind, profile_id),
        kwargs={"account_id": account_id, "marketplace": marketplace},
        daemon=True,
    )
    t.start()


# ---------------------------------------------------------------------------
# Profile discovery (mirrors acos_routes logic)
# ---------------------------------------------------------------------------

def _discover_profiles() -> dict[str, dict]:
    """Return {profile_id: {marketplace, account_id}} from all known sources."""
    seen: dict[str, dict] = {}

    # From acos_cache (already discovered profiles)
    for cp in db.list_acos_cache_profiles():
        pid = cp["profile_id"]
        if pid not in seen and cp.get("marketplace"):
            seen[pid] = {"marketplace": cp["marketplace"], "account_id": cp.get("account_id", "")}

    # From local campaign records
    for c in (db.list_amazon_campaigns() or []):
        pid = str(c["profile_id"])
        if pid not in seen:
            seen[pid] = {"marketplace": c["marketplace"], "account_id": c.get("amazon_account_id", "")}

    # From existing analytics caches
    for kind in ("keywords", "trends", "searchterms"):
        for cp in db.list_analytics_cache_profiles(kind):
            pid = cp["profile_id"]
            if pid not in seen and cp.get("marketplace"):
                seen[pid] = {"marketplace": cp["marketplace"], "account_id": cp.get("account_id", "")}

    return seen


# ---------------------------------------------------------------------------
# Data aggregation helpers
# ---------------------------------------------------------------------------

def _collect_data(kind: str, profiles: dict[str, dict], window: int) -> tuple[str, list[dict]]:
    """Check caches for all profiles, trigger stale fetches, return (status, merged_rows)."""
    any_fetching = False
    any_error = False
    all_rows: list[dict] = []
    data_col_map = {
        "keywords": "keywords_json",
        "trends": "trends_json",
        "searchterms": "terms_json",
    }
    col = data_col_map[kind]

    for pid, info in profiles.items():
        cache = db.get_analytics_cache(pid, kind)
        stale = (
            not cache
            or cache["status"] == "idle"
            or (cache["status"] == "ready" and time.time() - cache["fetched_at"] > CACHE_TTL)
        )
        if stale and (not cache or cache.get("status") != "fetching"):
            _trigger_fetch(kind, pid, account_id=info["account_id"], marketplace=info["marketplace"])

        if not cache:
            any_fetching = True
            continue

        s = cache["status"]
        if s == "fetching":
            any_fetching = True
        elif s == "error":
            any_error = True

        try:
            rows = json.loads(cache.get(col) or "[]")
        except Exception:
            rows = []
        # Tag each row with its source so the frontend can filter and the
        # bid-apply flow knows which profile/account/marketplace to write to.
        for row in rows:
            row.setdefault("_marketplace", info["marketplace"])
            row.setdefault("_profile_id", pid)
            row.setdefault("_account_id", info["account_id"])
        all_rows.extend(rows)

    overall = "fetching" if any_fetching else ("error" if any_error else "ready")
    if not profiles:
        overall = "no_campaigns"
    return overall, all_rows


# ---------------------------------------------------------------------------
# Bid apply helper
# ---------------------------------------------------------------------------

def _apply_one(item: dict) -> dict:
    """Validate one apply request and push it to Amazon. Never raises."""
    entity_id = str(item.get("entity_id") or "").strip()
    profile_id = str(item.get("profile_id") or "").strip()
    marketplace = str(item.get("marketplace") or "").strip().upper()
    account_id = (item.get("account_id") or "").strip() or None
    is_keyword = bool(item.get("is_keyword", True))

    # Echo back enough to identify the row in the UI.
    base = {"entity_id": entity_id, "keyword": item.get("keyword", "")}

    if not entity_id or not profile_id or not marketplace:
        return {**base, "ok": False,
                "error": "Missing entity_id / profile_id / marketplace — can't apply automatically."}

    # Resolve the change: explicit new_bid wins over a percentage delta.
    new_bid = item.get("new_bid")
    pct = item.get("bid_change_pct")
    current_bid = item.get("current_bid")
    try:
        new_bid = float(new_bid) if new_bid not in (None, "") else None
        pct = float(pct) if pct not in (None, "") else None
        current_bid = float(current_bid) if current_bid not in (None, "") else None
    except (TypeError, ValueError):
        return {**base, "ok": False, "error": "new_bid / bid_change_pct must be numbers."}

    if new_bid is None and pct is None:
        return {**base, "ok": False, "error": "Provide either new_bid or bid_change_pct."}

    try:
        from amazon_ads.bid_actions import apply_bid_change
        res = apply_bid_change(
            profile_id, entity_id,
            marketplace=marketplace,
            account_id=account_id,
            is_keyword=is_keyword,
            new_bid=new_bid,
            bid_change_pct=pct,
            current_bid=current_bid,
        )
        return {**base, "ok": True, **res}
    except Exception as exc:
        return {**base, "ok": False, "error": str(exc)[:300]}


def _apply_one_negative(item: dict) -> dict:
    """Validate one negative-keyword request and add it on Amazon. Never raises."""
    term = str(item.get("search_term") or "").strip()
    profile_id = str(item.get("profile_id") or "").strip()
    marketplace = str(item.get("marketplace") or "").strip().upper()
    account_id = (item.get("account_id") or "").strip() or None
    campaign_id = str(item.get("campaign_id") or "").strip()
    ad_group_id = str(item.get("ad_group_id") or "").strip()
    match_type = str(item.get("negative_match_type") or "NEGATIVE_EXACT").strip().upper()

    base = {"search_term": term, "ad_group_id": ad_group_id}

    if not term or not profile_id or not marketplace or not campaign_id or not ad_group_id:
        return {**base, "ok": False,
                "error": "Missing search_term / profile_id / marketplace / campaign_id / ad_group_id."}

    try:
        from amazon_ads.negatives import add_negative_keyword
        neg_id = add_negative_keyword(
            profile_id,
            marketplace=marketplace,
            account_id=account_id,
            campaign_id=campaign_id,
            ad_group_id=ad_group_id,
            keyword_text=term,
            match_type=match_type,
        )
        return {**base, "ok": True, "negative_keyword_id": neg_id}
    except Exception as exc:
        return {**base, "ok": False, "error": str(exc)[:300]}


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(app) -> None:

    @app.get("/analytics")
    def analytics_page():
        return render_template("analytics.html")

    # ---- Overview (reuses acos_cache data already collected by acos_routes) --
    @app.get("/api/analytics/overview")
    def analytics_overview():
        window = int(request.args.get("window", 14))
        if window not in (7, 14, 30):
            return jsonify({"error": "window must be 7, 14, or 30"}), 400

        # Pull from existing acos dashboard logic
        from acos_routes import _build_dashboard_response
        return jsonify(_build_dashboard_response(window))

    # ---- Keywords ----
    @app.get("/api/analytics/keywords")
    def analytics_keywords():
        window = int(request.args.get("window", 14))
        if window not in (7, 14, 30):
            return jsonify({"error": "window must be 7, 14, or 30"}), 400

        profiles = _discover_profiles()
        status, rows = _collect_data("keywords", profiles, window)
        # Sort by spend desc
        rows.sort(key=lambda r: float(r.get("_spend") or 0), reverse=True)
        return jsonify({"status": status, "window_days": window, "keywords": rows})

    # ---- Trends ----
    @app.get("/api/analytics/trends")
    def analytics_trends():
        profiles = _discover_profiles()
        status, rows = _collect_data("trends", profiles, 14)

        # Merge across profiles: aggregate by date
        by_date: dict[str, dict] = {}
        for row in rows:
            d = row.get("date") or ""
            if not d:
                continue
            if d not in by_date:
                by_date[d] = {"date": d, "spend": 0.0, "sales7d": 0.0, "sales14d": 0.0, "sales30d": 0.0, "clicks": 0, "impressions": 0}
            by_date[d]["spend"] = round(by_date[d]["spend"] + float(row.get("spend") or 0), 2)
            by_date[d]["sales7d"] = round(by_date[d]["sales7d"] + float(row.get("sales7d") or 0), 2)
            by_date[d]["sales14d"] = round(by_date[d]["sales14d"] + float(row.get("sales14d") or 0), 2)
            by_date[d]["sales30d"] = round(by_date[d]["sales30d"] + float(row.get("sales30d") or 0), 2)
            by_date[d]["clicks"] += int(row.get("clicks") or 0)
            by_date[d]["impressions"] += int(row.get("impressions") or 0)

        trend_rows = sorted(by_date.values(), key=lambda r: r["date"])
        return jsonify({"status": status, "trends": trend_rows})

    # ---- Search Terms ----
    @app.get("/api/analytics/search-terms")
    def analytics_search_terms():
        window = int(request.args.get("window", 14))
        if window not in (7, 14, 30):
            return jsonify({"error": "window must be 7, 14, or 30"}), 400

        profiles = _discover_profiles()
        status, rows = _collect_data("searchterms", profiles, window)
        rows.sort(key=lambda r: float(r.get("_spend") or 0), reverse=True)
        return jsonify({"status": status, "window_days": window, "search_terms": rows})

    # ---- Bid Suggestions ----
    @app.get("/api/analytics/bid-suggestions")
    def analytics_bid_suggestions():
        window = int(request.args.get("window", 14))
        target_acos = float(request.args.get("target_acos", 30))
        if window not in (7, 14, 30):
            return jsonify({"error": "window must be 7, 14, or 30"}), 400

        profiles = _discover_profiles()
        kw_status, kw_rows = _collect_data("keywords", profiles, window)

        from amazon_ads.reporting import compute_bid_suggestions
        suggestions = compute_bid_suggestions(kw_rows, target_acos=target_acos, window=window)
        return jsonify({
            "status": kw_status,
            "window_days": window,
            "target_acos": target_acos,
            "suggestions": suggestions,
        })

    # ---- Refresh ----
    @app.post("/api/analytics/refresh")
    def analytics_refresh():
        """Trigger fresh fetch for all analytics data types across all profiles."""
        body = request.get_json(silent=True) or {}
        kinds = body.get("kinds") or ["keywords", "trends", "searchterms"]

        # Auto-discover profiles from all sources including Amazon Profiles API
        seen: dict[str, dict] = {}
        try:
            from amazon_ads.profiles import list_all_profiles
            from acos_routes import _country_to_marketplace
            accounts = db.list_amazon_ads_accounts() or []
            for acct in accounts:
                try:
                    profiles_by_region = list_all_profiles(account_id=acct["id"])
                    for _region, profile_list in profiles_by_region.items():
                        for prof in profile_list:
                            pid = str(prof.get("profileId") or "")
                            cc = prof.get("countryCode") or ""
                            mp = _country_to_marketplace(cc) if cc else ""
                            if pid and mp:
                                seen[pid] = {"marketplace": mp, "account_id": acct["id"]}
                except Exception:
                    pass
        except Exception:
            pass

        # Fallback: known profiles
        for pid, info in _discover_profiles().items():
            if pid not in seen:
                seen[pid] = info

        for pid, info in seen.items():
            for kind in kinds:
                db.set_analytics_cache_status(pid, kind, "idle")
                _trigger_fetch(kind, pid, account_id=info["account_id"], marketplace=info["marketplace"])

        return jsonify({"ok": True, "status": "fetching", "profiles": list(seen.keys()), "kinds": kinds})

    # ---- Apply a single bid change ----
    @app.post("/api/analytics/apply-bid")
    def analytics_apply_bid():
        """Push one bid change to Amazon.

        Body: {
            profile_id, marketplace, entity_id, is_keyword,
            account_id?,                    # blank → .env default token
            bid_change_pct? | new_bid?,     # one of the two
            current_bid?                    # optional, saves a lookup
        }
        """
        body = request.get_json(silent=True) or {}
        result = _apply_one(body)
        code = 200 if result.get("ok") else 400
        return jsonify(result), code

    # ---- Apply many bid changes at once ----
    @app.post("/api/analytics/apply-bids")
    def analytics_apply_bids():
        """Bulk apply. Body: {suggestions: [ {same shape as apply-bid}, ... ]}."""
        body = request.get_json(silent=True) or {}
        items = body.get("suggestions") or []
        if not isinstance(items, list) or not items:
            return jsonify({"error": "suggestions (non-empty list) is required"}), 400

        results = [_apply_one(item) for item in items]
        applied = sum(1 for r in results if r.get("ok"))
        return jsonify({
            "ok": True,
            "applied": applied,
            "failed": len(results) - applied,
            "results": results,
        })

    # ---- Negative-keyword suggestions (wasteful search terms) ----
    @app.get("/api/analytics/negative-suggestions")
    def analytics_negative_suggestions():
        window = int(request.args.get("window", 14))
        target_acos = float(request.args.get("target_acos", 30))
        if window not in (7, 14, 30):
            return jsonify({"error": "window must be 7, 14, or 30"}), 400

        profiles = _discover_profiles()
        status, rows = _collect_data("searchterms", profiles, window)

        from amazon_ads.reporting import compute_negative_suggestions
        suggestions = compute_negative_suggestions(rows, target_acos=target_acos, window=window)
        return jsonify({
            "status": status,
            "window_days": window,
            "suggestions": suggestions,
        })

    # ---- Apply one / many negative keywords ----
    @app.post("/api/analytics/apply-negative")
    def analytics_apply_negative():
        body = request.get_json(silent=True) or {}
        result = _apply_one_negative(body)
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.post("/api/analytics/apply-negatives")
    def analytics_apply_negatives():
        body = request.get_json(silent=True) or {}
        items = body.get("suggestions") or []
        if not isinstance(items, list) or not items:
            return jsonify({"error": "suggestions (non-empty list) is required"}), 400
        results = [_apply_one_negative(item) for item in items]
        applied = sum(1 for r in results if r.get("ok"))
        return jsonify({
            "ok": True,
            "applied": applied,
            "failed": len(results) - applied,
            "results": results,
        })

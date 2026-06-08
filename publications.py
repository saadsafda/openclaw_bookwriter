"""Publications: post-write book state and routes.

A *publication* represents a book worldwide. Per-marketplace ASINs and URLs
live inside its ``marketplaces`` JSON column. This module owns:

  - the Launch tab page render
  - publication CRUD HTTP endpoints
  - zip-file upload + auto-detection of kindle/paperback/cover
  - multi-marketplace Sponsored Products fan-out

Routes are registered on the Flask app via ``register(app)``.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from flask import abort, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

import db as bookdb

ROOT_DIR = Path(__file__).resolve().parent
PUB_DIR = ROOT_DIR / "publications_data"
PUB_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pub_workdir(pub_id: str) -> Path:
    p = PUB_DIR / pub_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _detect_zip_contents(extract_dir: Path) -> dict[str, str]:
    """Inspect an extracted zip and find kindle/paperback/cover files.

    Returns paths as absolute strings.
    """
    kindle = ""
    paperback = ""
    cover = ""
    other_docx: list[Path] = []
    for path in sorted(extract_dir.rglob("*")):
        if not path.is_file():
            continue
        name = path.name.lower()
        suffix = path.suffix.lower()
        if suffix == ".docx":
            if "kindle" in name and not kindle:
                kindle = str(path)
            elif "paperback" in name and not paperback:
                paperback = str(path)
            else:
                other_docx.append(path)
        elif suffix in (".jpg", ".jpeg", ".png", ".webp"):
            if not cover or "cover" in name or "front" in name:
                cover = str(path)
    # Fallbacks when names don't include kindle/paperback hints
    if not kindle and other_docx:
        kindle = str(other_docx.pop(0))
    if not paperback and other_docx:
        paperback = str(other_docx.pop(0))
    return {
        "kindle_docx_path": kindle,
        "paperback_docx_path": paperback,
        "front_cover_path": cover,
    }


def _marketplace_default_budget(marketplace: str) -> float:
    return {
        "US": 5.0,   # USD
        "CA": 7.0,   # CAD
        "UK": 4.0,   # GBP
        "AU": 8.0,   # AUD
    }.get(marketplace.upper(), 5.0)


def _marketplace_default_bid(marketplace: str) -> float:
    return {
        "US": 0.50,
        "CA": 0.70,
        "UK": 0.40,
        "AU": 0.80,
    }.get(marketplace.upper(), 0.50)


def _amazon_url(marketplace: str, asin: str) -> str:
    domain = {
        "US": "amazon.com",
        "CA": "amazon.ca",
        "UK": "amazon.co.uk",
        "AU": "amazon.com.au",
    }.get(marketplace.upper(), "amazon.com")
    return f"https://{domain}/dp/{asin}"


def auto_create_from_book(book: dict[str, Any]) -> str:
    """Auto-create a draft publication from a finished writer book.

    Idempotent: if a publication already exists for this book, returns its id
    without creating a new one.
    """
    existing = bookdb.find_publication_by_book(book["id"])
    if existing:
        return existing["id"]
    pub_id = uuid.uuid4().hex[:12]
    listing = book.get("listing") or {}
    bookdb.save_publication(
        pub_id,
        book_id=book["id"],
        title=book.get("title") or listing.get("title") or "",
        subtitle=listing.get("subtitle") or "",
        description=listing.get("description") or "",
        categories=listing.get("categories") or [],
        status="draft",
        marketplaces={},
        kindle_docx_path=book.get("kindle_docx") or "",
        paperback_docx_path=book.get("paperback_docx") or "",
    )
    return pub_id


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(app) -> None:
    """Attach all Launch-tab routes to the given Flask app."""

    # -- Page render -------------------------------------------------------
    @app.get("/launch")
    def launch_page():  # noqa: ANN202
        return render_template("launch.html")

    # -- List / create publications ---------------------------------------
    @app.get("/api/publications")
    def list_pubs():  # noqa: ANN202
        return jsonify({"publications": bookdb.list_publications()})

    @app.get("/api/publications/<pub_id>")
    def get_pub(pub_id: str):  # noqa: ANN202
        pub = bookdb.get_publication(pub_id)
        if not pub:
            abort(404)
        # Attach campaign history for convenience
        pub["campaigns"] = bookdb.list_amazon_campaigns(publication_id=pub_id)
        return jsonify({"publication": pub})

    @app.post("/api/publications")
    def create_pub():  # noqa: ANN202
        body = request.get_json(silent=True) or {}
        book_id = (body.get("book_id") or "").strip()
        if book_id:
            book = bookdb.get_book(book_id)
            if not book:
                return jsonify({"error": "book_id not found"}), 404
            pub_id = auto_create_from_book(book)
            return jsonify({"id": pub_id, "publication": bookdb.get_publication(pub_id)})
        # Manual create
        pub_id = uuid.uuid4().hex[:12]
        bookdb.save_publication(
            pub_id,
            title=(body.get("title") or "").strip(),
            subtitle=(body.get("subtitle") or "").strip(),
            description=(body.get("description") or "").strip(),
            categories=body.get("categories") or [],
            status="draft",
        )
        return jsonify({"id": pub_id, "publication": bookdb.get_publication(pub_id)})

    @app.post("/api/publications/<pub_id>")
    def update_pub(pub_id: str):  # noqa: ANN202
        body = request.get_json(silent=True) or {}
        if not bookdb.get_publication(pub_id):
            abort(404)
        # Enrich marketplaces: if URL missing but ASIN present, infer URL
        if isinstance(body.get("marketplaces"), dict):
            for mp, info in list(body["marketplaces"].items()):
                if isinstance(info, dict) and info.get("asin") and not info.get("url"):
                    info["url"] = _amazon_url(mp, info["asin"])
        bookdb.update_publication(pub_id, **body)
        return jsonify({"publication": bookdb.get_publication(pub_id)})

    @app.delete("/api/publications/<pub_id>")
    def del_pub(pub_id: str):  # noqa: ANN202
        ok = bookdb.delete_publication(pub_id)
        return jsonify({"ok": ok})

    # -- ZIP upload --------------------------------------------------------
    @app.post("/api/publications/upload-zip")
    def upload_zip():  # noqa: ANN202
        if "zip" not in request.files:
            return jsonify({"error": "missing 'zip' file field"}), 400
        f = request.files["zip"]
        if not f or not f.filename:
            return jsonify({"error": "empty filename"}), 400
        filename = secure_filename(f.filename)
        if not filename.lower().endswith(".zip"):
            return jsonify({"error": "file must be a .zip"}), 400

        pub_id = uuid.uuid4().hex[:12]
        workdir = _pub_workdir(pub_id)
        zip_path = workdir / filename
        f.save(zip_path)

        # Extract
        extract_dir = workdir / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                # Defend against zip-slip
                for member in zf.infolist():
                    target = (extract_dir / member.filename).resolve()
                    if not str(target).startswith(str(extract_dir.resolve())):
                        return jsonify({"error": "unsafe zip member"}), 400
                zf.extractall(extract_dir)
        except zipfile.BadZipFile:
            shutil.rmtree(workdir, ignore_errors=True)
            return jsonify({"error": "not a valid zip"}), 400

        detected = _detect_zip_contents(extract_dir)

        # Optional metadata.json
        meta_title = ""
        meta_subtitle = ""
        meta_description = ""
        meta_categories: list[Any] = []
        meta_marketplaces: dict[str, Any] = {}
        meta_file = next(extract_dir.rglob("metadata.json"), None)
        if meta_file and meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                meta_title = (meta.get("title") or "").strip()
                meta_subtitle = (meta.get("subtitle") or "").strip()
                meta_description = (meta.get("description") or "").strip()
                meta_categories = meta.get("categories") or []
                if isinstance(meta.get("marketplaces"), dict):
                    meta_marketplaces = meta["marketplaces"]
            except Exception:
                pass

        # Title fallback: derive from zip filename
        if not meta_title:
            meta_title = Path(filename).stem.replace("_", " ").replace("-", " ").strip()

        bookdb.save_publication(
            pub_id,
            title=meta_title,
            subtitle=meta_subtitle,
            description=meta_description,
            categories=meta_categories,
            status="draft",
            marketplaces=meta_marketplaces,
            source_zip_path=str(zip_path),
            **detected,
        )
        return jsonify({
            "id": pub_id,
            "detected": detected,
            "publication": bookdb.get_publication(pub_id),
        })

    # -- File downloads ----------------------------------------------------
    @app.get("/api/publications/<pub_id>/file/<kind>")
    def download_pub_file(pub_id: str, kind: str):  # noqa: ANN202
        pub = bookdb.get_publication(pub_id)
        if not pub:
            abort(404)
        path_field = {
            "kindle": "kindle_docx_path",
            "paperback": "paperback_docx_path",
            "cover": "front_cover_path",
            "zip": "source_zip_path",
        }.get(kind)
        if not path_field:
            abort(404)
        p = pub.get(path_field) or ""
        if not p or not Path(p).exists():
            abort(404)
        return send_file(p, as_attachment=True, download_name=Path(p).name)

    # -- Multi-marketplace ads launch -------------------------------------
    @app.post("/api/publications/<pub_id>/amazon-ads/launch")
    def launch_ads(pub_id: str):  # noqa: ANN202
        """Launch one Sponsored Products campaign per marketplace, in one call.

        Two body shapes are accepted:

        1) Structured (preferred):
           {
             "account_id": "default",                # which company's ad account
             "type": "auto" | "keyword" | "category" | "asin",
             "state": "PAUSED" | "ENABLED",
             "bidding_strategy": "LEGACY_FOR_SALES" | "AUTO_FOR_SALES" | "MANUAL",
             "name_template": "{{title}} - {{type}} - {{country}}",
             "dry_run": false,
             "marketplaces": [
               {"country": "US", "default_bid": 1.00, "daily_budget": 5,
                "asin": "B0...", "name_override": ""},
               ...
             ],
             "keywords": [...]                        # for keyword type
             "category_ids": [...]                    # for category type
             "target_asins": [...]                    # for asin type
           }

        2) Legacy flat shape (still works for older callers):
           {"marketplaces": ["US","UK"], "default_bid_US": 1.0, "default_bid_UK": 1.2, ...}
        """
        pub = bookdb.get_publication(pub_id)
        if not pub:
            abort(404)
        body = request.get_json(silent=True) or {}
        ctype = (body.get("type") or "auto").lower()
        state = (body.get("state") or "PAUSED").upper()
        bidding_strategy = (body.get("bidding_strategy") or "LEGACY_FOR_SALES").upper()
        if bidding_strategy not in ("LEGACY_FOR_SALES", "AUTO_FOR_SALES", "MANUAL"):
            return jsonify({
                "error": "bidding_strategy must be LEGACY_FOR_SALES, AUTO_FOR_SALES, or MANUAL",
            }), 400
        dry_run = bool(body.get("dry_run"))
        name_template = (body.get("name_template")
                         or "{{title}} - {{type}} - {{country}}")

        # Which company's ad account to use.
        account_id = (body.get("account_id")
                      or pub.get("amazon_account_id")
                      or bookdb.get_default_amazon_ads_account_id()
                      or "")
        if not account_id:
            return jsonify({"error": "no Amazon Ads account configured"}), 400
        if not bookdb.get_amazon_ads_account(account_id, include_token=False):
            return jsonify({"error": f"account '{account_id}' not found"}), 400

        marketplaces_in = body.get("marketplaces") or []
        if not isinstance(marketplaces_in, list) or not marketplaces_in:
            return jsonify({
                "error": "marketplaces (list) is required",
            }), 400

        # Normalize to a list of per-marketplace dicts regardless of input shape.
        normalized: list[dict[str, Any]] = []
        for entry in marketplaces_in:
            if isinstance(entry, str):
                mp = entry.upper()
                normalized.append({
                    "country": mp,
                    "default_bid": float(body.get(f"default_bid_{mp}",
                                                 body.get("default_bid",
                                                          _marketplace_default_bid(mp)))),
                    "daily_budget": float(body.get(f"daily_budget_{mp}",
                                                   body.get("daily_budget",
                                                            _marketplace_default_budget(mp)))),
                    "asin": "",            # resolve from publication later
                    "name_override": "",
                })
            elif isinstance(entry, dict):
                mp = (entry.get("country") or entry.get("marketplace") or "").upper()
                if not mp:
                    continue
                normalized.append({
                    "country": mp,
                    "default_bid": float(entry.get("default_bid",
                                                   _marketplace_default_bid(mp))),
                    "daily_budget": float(entry.get("daily_budget",
                                                    _marketplace_default_budget(mp))),
                    "asin": (entry.get("asin") or "").strip(),
                    "name_override": (entry.get("name_override") or "").strip(),
                    "profile_id": entry.get("profile_id"),  # optional caller override
                })

        if not normalized:
            return jsonify({"error": "no usable marketplace entries"}), 400

        try:
            from amazon_ads import campaigns as _campaigns
            from amazon_ads import profiles as _profiles
        except Exception as exc:
            return jsonify({"error": f"amazon_ads module unavailable: {exc}"}), 500

        # Cache profile lookups per marketplace within this account.
        profile_cache: dict[str, int | None] = {}

        def _resolve_profile(mp: str) -> int | None:
            mp = mp.upper()
            if mp in profile_cache:
                return profile_cache[mp]
            try:
                profs = _profiles.list_profiles(mp, account_id=account_id)
                pid = _profiles.find_profile_id(profs, mp)
            except Exception:
                pid = None
            profile_cache[mp] = pid
            return pid

        title = (pub.get("title") or "Publication").strip()

        def _render_name(country: str, override: str) -> str:
            if override:
                return override[:128]
            rendered = (
                name_template
                .replace("{{title}}", title)
                .replace("{{type}}", ctype.capitalize())
                .replace("{{country}}", country)
                .replace("{{ts}}", str(int(time.time())))
            )
            return rendered[:128]

        per_mp_results: list[dict[str, Any]] = []
        for entry in normalized:
            mp = entry["country"]
            asin = entry["asin"] or (
                (pub.get("marketplaces") or {}).get(mp, {}) or {}
            ).get("asin", "")
            if not asin:
                per_mp_results.append({
                    "marketplace": mp, "ok": False,
                    "error": "no ASIN configured for this marketplace",
                })
                continue
            profile_id = entry.get("profile_id") or _resolve_profile(mp)
            if not profile_id:
                per_mp_results.append({
                    "marketplace": mp, "ok": False,
                    "error": f"no profile in account '{account_id}' for {mp}",
                })
                continue

            daily_budget = float(entry["daily_budget"])
            default_bid = float(entry["default_bid"])
            name = _render_name(mp, entry["name_override"])

            if dry_run:
                per_mp_results.append({
                    "marketplace": mp, "ok": True, "dry_run": True,
                    "preview": {
                        "name": name,
                        "asin": asin,
                        "profile_id": str(profile_id),
                        "daily_budget": daily_budget,
                        "default_bid": default_bid,
                        "bidding_strategy": bidding_strategy,
                        "state": state,
                        "type": ctype,
                        "account_id": account_id,
                    },
                })
                continue

            common = dict(
                marketplace=mp,
                profile_id=profile_id,
                name=name,
                asins=[asin],
                daily_budget=daily_budget,
                default_bid=default_bid,
                state=state,
                bidding_strategy=bidding_strategy,
                account_id=account_id,
            )

            try:
                if ctype == "auto":
                    result = _campaigns.create_auto_campaign(**common)
                elif ctype == "keyword":
                    keywords = body.get("keywords") or []
                    if not keywords:
                        raise ValueError("keywords required for keyword type")
                    result = _campaigns.create_keyword_campaign(
                        **common,
                        keywords=keywords,
                        match_types=tuple(body.get("match_types") or ("EXACT", "PHRASE", "BROAD")),
                        negative_keywords=body.get("negative_keywords") or [],
                    )
                elif ctype == "category":
                    cats = body.get("category_ids") or []
                    if not cats:
                        raise ValueError("category_ids required for category type")
                    result = _campaigns.create_category_campaign(**common, category_ids=cats)
                elif ctype == "asin":
                    targets = body.get("target_asins") or []
                    if not targets:
                        raise ValueError("target_asins required for asin type")
                    result = _campaigns.create_asin_campaign(**common, target_asins=targets)
                else:
                    raise ValueError(f"unknown campaign type: {ctype}")
            except Exception as exc:
                per_mp_results.append({"marketplace": mp, "ok": False, "error": str(exc)})
                continue

            # Persist
            try:
                bookdb.save_amazon_campaign(
                    campaign_id=str(result["campaignId"]),
                    book_id=pub.get("book_id") or "",
                    publication_id=pub_id,
                    marketplace=mp,
                    profile_id=str(profile_id),
                    campaign_type=ctype,
                    name=name,
                    asins=[asin],
                    ad_group_id=str(result.get("adGroupId", "")),
                    product_ad_ids=list(result.get("productAdIds", []) or []),
                    keyword_ids=list(result.get("keywordIds", []) or []),
                    negative_keyword_ids=list(result.get("negativeKeywordIds", []) or []),
                    target_ids=list(result.get("targetIds", []) or []),
                    daily_budget=daily_budget,
                    default_bid=default_bid,
                    state=state,
                    payload=result,
                    amazon_account_id=account_id,
                    bidding_strategy=bidding_strategy,
                )
            except Exception as exc:
                result["_db_warning"] = str(exc)

            per_mp_results.append({"marketplace": mp, "ok": True, "campaign": result})

        any_ok = any(r["ok"] for r in per_mp_results)
        return jsonify({
            "ok": any_ok,
            "account_id": account_id,
            "bidding_strategy": bidding_strategy,
            "dry_run": dry_run,
            "results": per_mp_results,
        })

    # -- Suggested bids (best-effort; never blocks a launch) --------------
    @app.post("/api/publications/<pub_id>/amazon-ads/suggested-bids")
    def suggested_bids(pub_id: str):  # noqa: ANN202
        """Return Amazon's suggested bid range per marketplace.

        Body:
          {
            "account_id": "default",                  # optional
            "type": "auto" | "keyword",
            "bidding_strategy": "LEGACY_FOR_SALES",    # optional
            "marketplaces": ["US","UK",...],
            "keywords": ["self help", ...]             # for keyword type
            "match_types": ["EXACT","PHRASE","BROAD"]  # for keyword type
          }

        Always 200. Each marketplace result has ``available: bool``; when false
        the UI simply shows "no suggestion" and the operator uses their own bid.
        """
        pub = bookdb.get_publication(pub_id)
        if not pub:
            abort(404)
        body = request.get_json(silent=True) or {}
        ctype = (body.get("type") or "auto").lower()
        bidding_strategy = (body.get("bidding_strategy") or "LEGACY_FOR_SALES").upper()
        keywords = [str(k).strip() for k in (body.get("keywords") or []) if str(k).strip()]
        match_types = tuple(body.get("match_types") or ("EXACT", "PHRASE", "BROAD"))

        account_id = (body.get("account_id")
                      or pub.get("amazon_account_id")
                      or bookdb.get_default_amazon_ads_account_id()
                      or "")
        if not account_id:
            return jsonify({"error": "no Amazon Ads account configured"}), 400

        mps = [str(m).upper() for m in (body.get("marketplaces") or []) if str(m).strip()]
        if not mps:
            return jsonify({"error": "marketplaces (list) is required"}), 400

        try:
            from amazon_ads import bids as _bids
            from amazon_ads import profiles as _profiles
        except Exception as exc:
            return jsonify({"error": f"amazon_ads module unavailable: {exc}"}), 500

        results: list[dict[str, Any]] = []
        for mp in mps:
            try:
                profs = _profiles.list_profiles(mp, account_id=account_id)
                profile_id = _profiles.find_profile_id(profs, mp)
            except Exception:
                profile_id = None
            if not profile_id:
                results.append({"marketplace": mp, "available": False,
                                "reason": f"no profile for {mp}"})
                continue
            if ctype == "keyword":
                rec = _bids.suggest_keyword_bids(
                    profile_id, mp, keywords, match_types,
                    account_id=account_id, bidding_strategy=bidding_strategy,
                )
            else:
                rec = _bids.suggest_auto_bids(
                    profile_id, mp,
                    account_id=account_id, bidding_strategy=bidding_strategy,
                )
            rec["marketplace"] = mp
            results.append(rec)

        return jsonify({"ok": True, "account_id": account_id, "results": results})

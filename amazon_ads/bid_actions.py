"""Apply bid changes to existing Sponsored Products keywords and targets (v3).

The Bid Optimizer produces suggestions like "reduce this keyword's bid 20%".
This module turns a suggestion into a real change on Amazon:

    1. Look up the current bid (the reporting API never returns it)
    2. Compute the new bid from a percentage delta (or take an absolute value)
    3. Clamp to Amazon's minimum, then PUT it back

Manual keywords (match type EXACT / PHRASE / BROAD) live under /sp/keywords.
Auto and product-targeting clauses live under /sp/targets. We pick the right
endpoint from the suggestion's match type.

(Not to be confused with ``bids.py``, which suggests bids for *new* campaigns
during launch. This module edits bids on *existing* keywords/targets.)
"""

from __future__ import annotations

from typing import Any, Iterable

from .client import AmazonAdsClient

# Vendor content types for SP v3 (must match what campaigns.py uses)
_CT_KEYWORD = "application/vnd.spKeyword.v3+json"
_CT_TARGET = "application/vnd.spTargetingClause.v3+json"

# Amazon's Sponsored Products minimum bid. Local currencies have a similar
# floor (£0.02, A$0.02, C$0.02…), so 0.02 is a safe lower clamp everywhere.
MIN_BID = 0.02

# Don't let a runaway percentage push a bid absurdly high.
MAX_BID = 1000.0


def is_keyword_match(match_type: str | None) -> bool:
    """True for manual keyword match types; False for auto/product targets."""
    return (match_type or "").upper() in ("EXACT", "PHRASE", "BROAD")


# ---------------------------------------------------------------------------
# Reading current bids
# ---------------------------------------------------------------------------


def get_keyword_bids(client: AmazonAdsClient, keyword_ids: Iterable[str]) -> dict[str, float]:
    """Return {keywordId: bid} for the given manual keyword ids."""
    ids = [str(k) for k in keyword_ids if k]
    if not ids:
        return {}
    resp = client.post(
        "/sp/keywords/list",
        json={"keywordIdFilter": {"include": ids}, "maxResults": len(ids) + 10},
        content_type=_CT_KEYWORD,
        accept=_CT_KEYWORD,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"List keywords failed ({resp.status_code}): {resp.text[:300]}")
    out: dict[str, float] = {}
    for kw in resp.json().get("keywords", []):
        kid = str(kw.get("keywordId") or "")
        if kid and kw.get("bid") is not None:
            out[kid] = float(kw["bid"])
    return out


def get_target_bids(client: AmazonAdsClient, target_ids: Iterable[str]) -> dict[str, float]:
    """Return {targetId: bid} for the given targeting-clause ids."""
    ids = [str(t) for t in target_ids if t]
    if not ids:
        return {}
    resp = client.post(
        "/sp/targets/list",
        json={"targetIdFilter": {"include": ids}, "maxResults": len(ids) + 10},
        content_type=_CT_TARGET,
        accept=_CT_TARGET,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"List targets failed ({resp.status_code}): {resp.text[:300]}")
    out: dict[str, float] = {}
    for t in resp.json().get("targetingClauses", []):
        tid = str(t.get("targetId") or "")
        if tid and t.get("bid") is not None:
            out[tid] = float(t["bid"])
    return out


# ---------------------------------------------------------------------------
# Writing new bids
# ---------------------------------------------------------------------------


def _check_update(resp, key: str) -> None:
    """SP v3 PUT returns {'<key>': {'success': [...], 'error': [...]}}."""
    if resp.status_code >= 300:
        raise RuntimeError(f"Bid update failed ({resp.status_code}): {resp.text[:300]}")
    block = resp.json().get(key) or {}
    errors = block.get("error") or []
    if errors:
        # Surface Amazon's own error text so the UI can show it.
        msgs = []
        for e in errors:
            detail = e.get("errors") or e.get("message") or e
            msgs.append(str(detail))
        raise RuntimeError(f"Amazon rejected the bid change: {'; '.join(msgs)[:300]}")
    if not (block.get("success") or []):
        raise RuntimeError(f"Amazon returned no success for the bid change: {resp.text[:200]}")


def update_keyword_bid(client: AmazonAdsClient, keyword_id: str, new_bid: float) -> None:
    resp = client.put(
        "/sp/keywords",
        json={"keywords": [{"keywordId": str(keyword_id), "bid": round(float(new_bid), 2)}]},
        content_type=_CT_KEYWORD,
        accept=_CT_KEYWORD,
    )
    _check_update(resp, "keywords")


def update_target_bid(client: AmazonAdsClient, target_id: str, new_bid: float) -> None:
    resp = client.put(
        "/sp/targets",
        json={"targetingClauses": [{"targetId": str(target_id), "bid": round(float(new_bid), 2)}]},
        content_type=_CT_TARGET,
        accept=_CT_TARGET,
    )
    _check_update(resp, "targetingClauses")


# ---------------------------------------------------------------------------
# High-level: apply one suggestion
# ---------------------------------------------------------------------------


def apply_bid_change(
    profile_id: str | int,
    entity_id: str,
    *,
    marketplace: str,
    account_id: str | None = None,
    is_keyword: bool = True,
    new_bid: float | None = None,
    bid_change_pct: float | None = None,
    current_bid: float | None = None,
) -> dict[str, Any]:
    """Apply a bid change to one keyword or target.

    Provide either ``new_bid`` (absolute) or ``bid_change_pct`` (relative).
    For a percentage change we fetch the current bid first (unless supplied).

    Returns ``{old_bid, new_bid, applied: True}``.
    """
    if not entity_id:
        raise RuntimeError("No keyword/target id — this suggestion can't be applied automatically.")

    client = AmazonAdsClient(
        marketplace=marketplace,
        profile_id=str(profile_id),
        account_id=account_id,
    )

    # Resolve the current bid when we need it to compute a percentage delta.
    if new_bid is None:
        if current_bid is None:
            bids = (
                get_keyword_bids(client, [entity_id])
                if is_keyword
                else get_target_bids(client, [entity_id])
            )
            current_bid = bids.get(str(entity_id))
        if current_bid is None:
            raise RuntimeError("Could not read the current bid from Amazon to apply a percentage change.")
        if bid_change_pct is None:
            raise RuntimeError("Either new_bid or bid_change_pct is required.")
        computed = float(current_bid) * (1.0 + float(bid_change_pct) / 100.0)
    else:
        computed = float(new_bid)

    final_bid = max(MIN_BID, min(MAX_BID, round(computed, 2)))

    if is_keyword:
        update_keyword_bid(client, entity_id, final_bid)
    else:
        update_target_bid(client, entity_id, final_bid)

    return {"old_bid": current_bid, "new_bid": final_bid, "applied": True}

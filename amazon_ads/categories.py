"""Browse and search Amazon Ads category-targeting categories.

Used to discover the numeric category ids required by
``campaigns.create_category_campaign``.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Iterator

from .client import AmazonAdsClient

_CT = "application/vnd.spproducttargeting.v3+json"


def fetch_category_tree(marketplace: str, profile_id: int | str) -> list[dict]:
    """Return the full category tree for a marketplace as a list of nodes.

    Each node has the shape:
        {"id": int, "na": str, "ch": [...], "ta": bool}
    where ``ta`` indicates targetable (you can only target leaves where ta is True).
    """
    client = AmazonAdsClient(marketplace=marketplace, profile_id=profile_id)
    resp = client.get("/sp/targets/categories", accept=_CT)
    if resp.status_code >= 300:
        raise RuntimeError(
            f"GET /sp/targets/categories failed ({resp.status_code}): {resp.text[:200]}"
        )
    payload = resp.json()
    return json.loads(payload["CategoryTree"])


def walk_categories(
    nodes: Iterable[dict], path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], dict]]:
    """Yield (path, node) tuples for every node in the tree."""
    for n in nodes:
        node_path = path + (n.get("na", ""),)
        yield node_path, n
        for child in walk_categories(n.get("ch", []), node_path):
            yield child


def search_categories(
    marketplace: str,
    profile_id: int | str,
    query: str,
    *,
    targetable_only: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Find category leaves whose path or name match a substring.

    Returns a list of ``{"id", "name", "path", "targetable"}`` dicts.
    """
    tree = fetch_category_tree(marketplace, profile_id)
    q = query.lower()
    results: list[dict[str, Any]] = []
    for path, node in walk_categories(tree):
        if targetable_only and not node.get("ta"):
            continue
        if q in node.get("na", "").lower() or any(q in p.lower() for p in path):
            results.append(
                {
                    "id": str(node["id"]),
                    "name": node["na"],
                    "path": " > ".join(path),
                    "targetable": bool(node.get("ta")),
                }
            )
            if len(results) >= limit:
                break
    return results

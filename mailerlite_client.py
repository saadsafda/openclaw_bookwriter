"""
Thin wrapper around the MailerLite REST API (connect.mailerlite.com).

Only the endpoints we need for pushing draft launch campaigns:
- Ping / test connection (via GET /groups?limit=1)
- List groups (for the UI dropdown)
- Create a draft campaign (regular, one email, one or more groups)

Auth: Bearer token in `Authorization` header.
Docs: https://developers.mailerlite.com/docs/campaigns.html
"""
from __future__ import annotations

from typing import Any, Optional

try:
    import requests  # type: ignore
except ImportError:  # pragma: no cover
    requests = None  # lazy error on first use

API_BASE = "https://connect.mailerlite.com/api"
DEFAULT_TIMEOUT = 15  # seconds


class MailerLiteError(Exception):
    """Raised when MailerLite returns an error response or the request fails."""

    def __init__(self, message: str, *, status_code: Optional[int] = None,
                 payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class MailerLiteClient:
    def __init__(self, api_key: str, *, timeout: int = DEFAULT_TIMEOUT):
        if requests is None:
            raise MailerLiteError(
                "The `requests` package is required for MailerLite integration. "
                "Install it with: pip install requests"
            )
        if not api_key or not api_key.strip():
            raise MailerLiteError("MailerLite API key is empty.")
        self.api_key = api_key.strip()
        self.timeout = timeout

    # ------------------------------------------------------------------ http
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, params: Optional[dict] = None,
                 json_body: Optional[dict] = None) -> dict:
        url = f"{API_BASE}{path}"
        try:
            resp = requests.request(
                method, url,
                headers=self._headers(),
                params=params,
                json=json_body,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise MailerLiteError(f"Network error contacting MailerLite: {exc}")

        if resp.status_code == 401:
            raise MailerLiteError(
                "MailerLite rejected the API key (401 Unauthorized). "
                "Double-check the token in Settings.",
                status_code=401,
            )
        if resp.status_code == 403:
            raise MailerLiteError(
                "MailerLite denied access (403 Forbidden). "
                "Ensure the API key has campaign/group permissions.",
                status_code=403,
            )
        if resp.status_code == 429:
            raise MailerLiteError(
                "MailerLite rate-limited the request (429). Try again in a moment.",
                status_code=429,
            )
        if resp.status_code == 204 or not resp.content:
            return {}

        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}

        if not (200 <= resp.status_code < 300):
            msg = ""
            if isinstance(data, dict):
                msg = data.get("message") or ""
                errors = data.get("errors")
                if isinstance(errors, dict) and errors:
                    first = next(iter(errors.values()))
                    if isinstance(first, list) and first:
                        msg = f"{msg}: {first[0]}" if msg else first[0]
            raise MailerLiteError(
                msg or f"MailerLite returned HTTP {resp.status_code}",
                status_code=resp.status_code,
                payload=data,
            )
        return data if isinstance(data, dict) else {"data": data}

    # ---------------------------------------------------------------- public
    def test_connection(self) -> dict:
        """Lightweight ping: fetch a single group. Succeeds → creds OK."""
        self._request("GET", "/groups", params={"limit": 1})
        return {"ok": True}

    def list_groups(self, limit: int = 100) -> list[dict]:
        """Return list of subscriber groups (newest first), up to `limit`."""
        result = self._request("GET", "/groups", params={"limit": limit})
        data = result.get("data") or []
        out = []
        for g in data:
            if not isinstance(g, dict):
                continue
            out.append({
                "id": str(g.get("id", "")),
                "name": g.get("name", ""),
                "active_count": g.get("active_count", 0),
                "total": g.get("total", g.get("active_count", 0)),
            })
        return out

    def create_group(self, name: str) -> dict:
        """Create a subscriber group and return {id, name}."""
        result = self._request("POST", "/groups", json_body={"name": name.strip()[:255]})
        data = result.get("data") or {}
        return {"id": str(data.get("id", "")), "name": data.get("name", "")}

    def ensure_group(self, name: str) -> str:
        """Return the id of a group with this name, creating it if needed."""
        for g in self.list_groups(limit=200):
            if (g.get("name") or "").strip().lower() == name.strip().lower():
                return g["id"]
        return self.create_group(name)["id"]

    def upsert_subscriber(
        self, *, email: str, name: str = "", group_ids: Optional[list[str]] = None
    ) -> dict:
        """Create or update a subscriber and (optionally) add them to groups."""
        body: dict[str, Any] = {"email": email.strip()}
        if name:
            body["fields"] = {"name": name.strip()}
        if group_ids:
            body["groups"] = [str(g) for g in group_ids if str(g).strip()]
        result = self._request("POST", "/subscribers", json_body=body)
        data = result.get("data") or {}
        return {"id": str(data.get("id", "")), "email": data.get("email", email)}

    def create_draft_campaign(
        self,
        *,
        name: str,
        subject: str,
        from_email: str,
        from_name: str,
        html_content: Optional[str] = None,
        group_ids: Optional[list[str]] = None,
    ) -> dict:
        """Create a regular campaign in draft status. Returns {id, status, ...}."""
        if not subject.strip():
            raise MailerLiteError("Campaign subject is required.")
        if not from_email.strip():
            raise MailerLiteError(
                "A verified `from` email address is required to create a draft."
            )
        if not from_name.strip():
            raise MailerLiteError("A `from_name` is required to create a draft.")

        email: dict[str, Any] = {
            "subject": subject.strip()[:255],
            "from_name": from_name.strip()[:255],
            "from": from_email.strip(),
        }
        if html_content and html_content.strip():
            email["content"] = html_content

        payload: dict[str, Any] = {
            "name": name.strip()[:255] or subject.strip()[:255],
            "type": "regular",
            "emails": [email],
        }
        if group_ids:
            payload["groups"] = [str(g) for g in group_ids if str(g).strip()]

        result = self._request("POST", "/campaigns", json_body=payload)
        data = result.get("data") or {}
        return {
            "id": str(data.get("id", "")),
            "status": data.get("status", ""),
            "name": data.get("name", ""),
            "raw": data,
        }

"""Thin HTTP wrapper for Amazon Ads API calls.

Picks the correct regional host for the marketplace, attaches the LWA
access token, the client_id header, and the profile scope header.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from .auth import TokenManager, default_token_manager, get_token_manager
from .config import AmazonAdsConfig


class AmazonAdsClient:
    def __init__(
        self,
        marketplace: str,
        profile_id: int | str | None = None,
        token_manager: TokenManager | None = None,
        *,
        account_id: str | None = None,
    ) -> None:
        if token_manager is not None:
            self._tm = token_manager
        elif account_id is not None:
            self._tm = get_token_manager(account_id)
        else:
            self._tm = default_token_manager()
        self.marketplace = marketplace.upper()
        self.profile_id = str(profile_id) if profile_id is not None else None

    @property
    def account_id(self) -> str:
        return self._tm.account_id

    @property
    def config(self) -> AmazonAdsConfig:
        return self._tm.config

    @property
    def host(self) -> str:
        return self.config.host_for(self.marketplace)

    # ---- header building ----------------------------------------------------

    def _headers(
        self,
        *,
        require_profile: bool,
        content_type: str | None = None,
        accept: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._tm.get_access_token()}",
            "Amazon-Advertising-API-ClientId": self.config.client_id,
        }
        if require_profile:
            if not self.profile_id:
                raise RuntimeError(
                    f"profile_id is required for {self.marketplace} requests. "
                    "Call profiles.list_profiles() and pass the matching id."
                )
            headers["Amazon-Advertising-API-Scope"] = self.profile_id
        if content_type:
            headers["Content-Type"] = content_type
        if accept:
            headers["Accept"] = accept
        return headers

    # ---- core request -------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        require_profile: bool = True,
        content_type: str = "application/json",
        accept: str = "application/json",
        extra_headers: dict[str, str] | None = None,
        timeout: int = 60,
        retry_on_401: bool = True,
    ) -> requests.Response:
        url = f"{self.host}{path}"
        headers = self._headers(
            require_profile=require_profile,
            content_type=content_type if json is not None else None,
            accept=accept,
        )
        if extra_headers:
            headers.update(extra_headers)

        resp = requests.request(
            method.upper(),
            url,
            headers=headers,
            json=json,
            params=params,
            timeout=timeout,
        )

        # Token expired between requests -> force refresh and retry once.
        if resp.status_code == 401 and retry_on_401:
            self._tm.get_access_token(force_refresh=True)
            return self.request(
                method,
                path,
                json=json,
                params=params,
                require_profile=require_profile,
                content_type=content_type,
                accept=accept,
                extra_headers=extra_headers,
                timeout=timeout,
                retry_on_401=False,
            )

        # Honor Amazon's rate limiting hint.
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "1"))
            time.sleep(retry_after)

        return resp

    # ---- convenience wrappers ----------------------------------------------

    def get(self, path: str, **kw) -> requests.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, json: Any = None, **kw) -> requests.Response:
        return self.request("POST", path, json=json, **kw)

    def put(self, path: str, json: Any = None, **kw) -> requests.Response:
        return self.request("PUT", path, json=json, **kw)

    def delete(self, path: str, **kw) -> requests.Response:
        return self.request("DELETE", path, **kw)

"""LWA access-token minting and in-process caching.

Refresh tokens are long-lived (years). Access tokens last ~60 minutes, so we
cache them and only re-mint when within the expiry buffer.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import requests

from .config import LWA_TOKEN_URL, AmazonAdsConfig, load_config

# Re-mint a little before actual expiry so in-flight requests don't 401.
_EXPIRY_BUFFER_SECONDS = 60


@dataclass
class _CachedToken:
    access_token: str
    expires_at: float  # epoch seconds


class TokenManager:
    """Thread-safe access-token cache backed by an LWA refresh token."""

    def __init__(
        self,
        config: AmazonAdsConfig | None = None,
        *,
        account_id: str | None = None,
    ) -> None:
        self._config = config or load_config(account_id)
        self._account_id = account_id or "default"
        self._lock = threading.Lock()
        self._cached: _CachedToken | None = None

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def config(self) -> AmazonAdsConfig:
        return self._config

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        if not self._config.refresh_token:
            raise RuntimeError(
                "LWA_REFRESH_TOKEN is not set. Run:\n"
                "    python -m amazon_ads.get_refresh_token\n"
                "and paste the printed token into .env"
            )

        with self._lock:
            now = time.time()
            if (
                not force_refresh
                and self._cached is not None
                and self._cached.expires_at - _EXPIRY_BUFFER_SECONDS > now
            ):
                return self._cached.access_token

            token, ttl = _exchange_refresh_token(
                self._config.client_id,
                self._config.client_secret,
                self._config.refresh_token,
            )
            self._cached = _CachedToken(
                access_token=token,
                expires_at=now + ttl,
            )
            return token


def _exchange_refresh_token(
    client_id: str, client_secret: str, refresh_token: str
) -> tuple[str, int]:
    """Exchange a refresh token for (access_token, ttl_seconds)."""
    resp = requests.post(
        LWA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"LWA token refresh failed ({resp.status_code}): {resp.text}"
        )
    payload = resp.json()
    return payload["access_token"], int(payload.get("expires_in", 3600))


# Per-account TokenManager registry — one LWA token cache per company.
_managers: dict[str, TokenManager] = {}
_managers_lock = threading.Lock()


def get_token_manager(account_id: str | None = None) -> TokenManager:
    key = (account_id or "").strip() or "__env__"
    with _managers_lock:
        if key not in _managers:
            _managers[key] = TokenManager(
                account_id=(account_id.strip() if account_id else None)
            )
        return _managers[key]


def default_token_manager() -> TokenManager:
    """Back-compat: a TokenManager seeded from the legacy LWA_REFRESH_TOKEN env."""
    return get_token_manager(None)


def reset_token_manager(account_id: str | None = None) -> None:
    """Drop the cached TokenManager for an account (e.g. after a token rotation)."""
    key = (account_id or "").strip() or "__env__"
    with _managers_lock:
        _managers.pop(key, None)

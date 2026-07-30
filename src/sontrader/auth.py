"""OAuth token management for the KIS open API.

KIS access tokens live for 24 hours and re-issuing invalidates the
previous token, so the token is cached on disk and reused until
shortly before expiry. Paper and real tokens are not interchangeable;
the cache records which base URL issued it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx

from sontrader.config import Settings

_EXPIRY_MARGIN = timedelta(minutes=10)
# KIS returns expiry as local (KST) wall-clock time, e.g. "2026-07-31 09:00:00".
_EXPIRY_FORMAT = "%Y-%m-%d %H:%M:%S"


class TokenManager:
    def __init__(self, settings: Settings, http: httpx.Client):
        self._settings = settings
        self._http = http

    def get_token(self) -> str:
        return self._read_cache() or self._issue()

    def _read_cache(self) -> str | None:
        try:
            data = json.loads(self._settings.token_cache.read_text())
        except (OSError, ValueError):
            return None
        if data.get("base_url") != self._settings.base_url:
            return None
        try:
            expires_at = datetime.strptime(data["expires_at"], _EXPIRY_FORMAT)
        except (KeyError, ValueError):
            return None
        if datetime.now() >= expires_at - _EXPIRY_MARGIN:
            return None
        return data.get("access_token") or None

    def _issue(self) -> str:
        response = self._http.post(
            "/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self._settings.app_key,
                "appsecret": self._settings.app_secret,
            },
        )
        response.raise_for_status()
        payload = response.json()
        token = payload["access_token"]
        self._write_cache(token, payload["access_token_token_expired"])
        return token

    def _write_cache(self, token: str, expires_at: str) -> None:
        path = self._settings.token_cache
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "access_token": token,
                    "expires_at": expires_at,
                    "base_url": self._settings.base_url,
                }
            )
        )
        path.chmod(0o600)

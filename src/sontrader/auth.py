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


class KisError(RuntimeError):
    """KIS가 본문으로 거절 사유를 알린 요청 (API-level failure).

    ``client.py``에서 재노출한다 — 호출자 대부분은 클라이언트만 알면 되고,
    여기 있는 이유는 인증 계층이 클라이언트를 import할 수 없어서다.
    """


def raise_for_kis_error(response: httpx.Response) -> None:
    """KIS가 본문에 담아 보낸 실패를 ``KisError``로 올린다.

    KIS는 자신이 진단한 오류도 HTTP 4xx/5xx와 함께 본문에 실어 보낸다.
    ``raise_for_status()``를 먼저 부르면 그 진단이 통째로 사라지고, 대신
    httpx 예외 메시지에 CANO가 든 전체 URL이 로그로 새어 나간다.

    본문 형식이 엔드포인트마다 다르다 — 토큰·접속키 발급은
    ``error_code``/``error_description``, 나머지 업무 API는
    ``rt_cd``/``msg_cd``/``msg1``을 쓴다. 둘 다 아닌 응답(게이트웨이가 만든
    HTML 오류 등)은 KIS가 만든 것이 아니므로 판단하지 않고 그냥 돌아간다 —
    호출자가 ``raise_for_status()``로 처리한다.
    """
    try:
        payload = response.json()
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    if payload.get("error_code"):
        description = str(payload.get("error_description", "")).strip()
        raise KisError(f"{payload['error_code']}: {description}")
    if payload.get("rt_cd", "0") != "0":
        raise KisError(f"{payload.get('msg_cd')}: {str(payload.get('msg1', '')).strip()}")


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
        raise_for_kis_error(response)
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


class ApprovalKeyManager:
    """웹소켓 접속키(실시간-000, ``/oauth2/Approval``) 발급·캐시.

    24시간 유효하지만 세션 연결 시 최초 1회만 쓰이고, 세션이 끊기지 않는
    한 재발급 없이 365일 계속 쓸 수 있다 — 그래도 재연결 시엔 유효한 키가
    있어야 하므로, `TokenManager`와 같은 이유(재발급 시 이전 키가 무효화될
    수 있고 유량도 아낀다)로 디스크에 캐시해 최대한 재사용한다.

    이 엔드포인트는 만료 시각을 응답에 주지 않으므로(``{"approval_key": ...}``
    뿐) 발급 시각 + 24시간을 직접 계산해 캐시한다.
    """

    _VALIDITY = timedelta(hours=24)

    def __init__(self, settings: Settings, http: httpx.Client):
        self._settings = settings
        self._http = http

    def get_key(self) -> str:
        return self._read_cache() or self._issue()

    def _read_cache(self) -> str | None:
        try:
            data = json.loads(self._settings.approval_key_cache.read_text())
        except (OSError, ValueError):
            return None
        if data.get("base_url") != self._settings.base_url:
            return None
        try:
            issued_at = datetime.strptime(data["issued_at"], _EXPIRY_FORMAT)
        except (KeyError, ValueError):
            return None
        if datetime.now() >= issued_at + self._VALIDITY - _EXPIRY_MARGIN:
            return None
        return data.get("approval_key") or None

    def _issue(self) -> str:
        response = self._http.post(
            "/oauth2/Approval",
            json={
                "grant_type": "client_credentials",
                "appkey": self._settings.app_key,
                # 필드명이 appsecret이 아니라 secretkey다 — 값은 같지만 KIS
                # 문서가 이 엔드포인트에서만 다른 이름을 쓴다.
                "secretkey": self._settings.app_secret,
            },
        )
        raise_for_kis_error(response)
        response.raise_for_status()
        key = response.json()["approval_key"]
        self._write_cache(key)
        return key

    def _write_cache(self, key: str) -> None:
        path = self._settings.approval_key_cache
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "approval_key": key,
                    "issued_at": datetime.now().strftime(_EXPIRY_FORMAT),
                    "base_url": self._settings.base_url,
                }
            )
        )
        path.chmod(0o600)

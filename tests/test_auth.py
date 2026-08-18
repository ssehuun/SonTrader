import json

import httpx
import pytest

from sontrader.auth import ApprovalKeyManager, KisError, TokenManager
from tests.conftest import TOKEN_RESPONSE


def make_http(settings, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth2/tokenP"
        calls.append(request)
        return httpx.Response(200, json=TOKEN_RESPONSE)

    return httpx.Client(base_url=settings.base_url, transport=httpx.MockTransport(handler))


def test_issues_and_caches_token(settings):
    calls = []
    manager = TokenManager(settings, make_http(settings, calls))

    assert manager.get_token() == "test-token"
    assert manager.get_token() == "test-token"
    assert len(calls) == 1  # second call served from the disk cache

    cached = json.loads(settings.token_cache.read_text())
    assert cached["access_token"] == "test-token"
    assert cached["base_url"] == settings.base_url


def test_expired_cache_is_reissued(settings):
    settings.token_cache.write_text(
        json.dumps(
            {
                "access_token": "stale",
                "expires_at": "2000-01-01 00:00:00",
                "base_url": settings.base_url,
            }
        )
    )
    calls = []
    manager = TokenManager(settings, make_http(settings, calls))

    assert manager.get_token() == "test-token"
    assert len(calls) == 1


def test_cache_from_other_environment_is_ignored(settings):
    settings.token_cache.write_text(
        json.dumps(
            {
                "access_token": "real-env-token",
                "expires_at": "2099-01-01 00:00:00",
                "base_url": "https://openapi.koreainvestment.com:9443",
            }
        )
    )
    calls = []
    manager = TokenManager(settings, make_http(settings, calls))

    assert manager.get_token() == "test-token"  # paper client must not reuse a real token
    assert len(calls) == 1


# --- ApprovalKeyManager (웹소켓 접속키) -----------------------------------------


def make_approval_http(settings, calls, *, response=None):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth2/Approval"
        body = json.loads(request.content)
        assert body["grant_type"] == "client_credentials"
        assert body["appkey"] == settings.app_key
        assert body["secretkey"] == settings.app_secret
        assert "appsecret" not in body
        calls.append(request)
        return httpx.Response(200, json=response or {"approval_key": "test-approval-key"})

    return httpx.Client(base_url=settings.base_url, transport=httpx.MockTransport(handler))


def test_issues_and_caches_approval_key(settings):
    calls = []
    manager = ApprovalKeyManager(settings, make_approval_http(settings, calls))

    assert manager.get_key() == "test-approval-key"
    assert manager.get_key() == "test-approval-key"
    assert len(calls) == 1  # second call served from the disk cache

    cached = json.loads(settings.approval_key_cache.read_text())
    assert cached["approval_key"] == "test-approval-key"
    assert cached["base_url"] == settings.base_url
    assert "issued_at" in cached


def test_approval_key_cache_older_than_24h_is_reissued(settings):
    settings.approval_key_cache.write_text(
        json.dumps(
            {
                "approval_key": "stale",
                "issued_at": "2000-01-01 00:00:00",
                "base_url": settings.base_url,
            }
        )
    )
    calls = []
    manager = ApprovalKeyManager(settings, make_approval_http(settings, calls))

    assert manager.get_key() == "test-approval-key"
    assert len(calls) == 1


def test_approval_key_cache_from_other_environment_is_ignored(settings):
    settings.approval_key_cache.write_text(
        json.dumps(
            {
                "approval_key": "real-env-key",
                "issued_at": "2099-01-01 00:00:00",
                "base_url": "https://openapi.koreainvestment.com:9443",
            }
        )
    )
    calls = []
    manager = ApprovalKeyManager(settings, make_approval_http(settings, calls))

    assert manager.get_key() == "test-approval-key"
    assert len(calls) == 1


def test_token_issuance_error_body_survives_403(settings):
    """토큰 발급 유량 제한(1분 1회)에 걸리면 KIS는 403 + error_code로 답한다.

    실전 전환 직후 캐시를 지우고 재발급을 시도했을 때 실제로 이 응답을
    받았고, 당시엔 raise_for_status()가 먼저 터져 원인이 보이지 않았다.
    발급 실패는 진단이 가장 필요한 지점이다 — 유량 제한인지, 앱키가
    틀렸는지, 시크릿이 만료됐는지에 따라 대응이 완전히 다르다.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error_code": "EGW00133",
                "error_description": "접근토큰 발급 잠시 후 다시 시도하세요(1분당 1회)",
            },
        )

    http = httpx.Client(base_url=settings.base_url, transport=httpx.MockTransport(handler))
    manager = TokenManager(settings, http)

    with pytest.raises(KisError, match="EGW00133"):
        manager.get_token()


def test_approval_key_issuance_error_body_survives(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"error_code": "EGW00133", "error_description": "잠시 후 다시 시도하세요"}
        )

    http = httpx.Client(base_url=settings.base_url, transport=httpx.MockTransport(handler))

    with pytest.raises(KisError, match="EGW00133"):
        ApprovalKeyManager(settings, http).get_key()


def test_non_kis_http_error_still_raises_http_status_error(settings):
    """error_code도 rt_cd도 없는 응답은 KIS가 만든 것이 아니다."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    http = httpx.Client(base_url=settings.base_url, transport=httpx.MockTransport(handler))

    with pytest.raises(httpx.HTTPStatusError):
        TokenManager(settings, http).get_token()


def test_token_cache_is_invalidated_when_the_app_key_changes(settings, tmp_path):
    """앱키를 바꾸면 캐시된 토큰을 재사용하지 않는다.

    KIS는 앱키가 바뀌면 이전 토큰을 무효화하는데, 그 상태로 쓰면
    "기간이 만료된 token"(EGW00123)이라는 엉뚱한 사유로 거절한다 — 캐시는
    아직 유효 기간이 남아 있어 원인을 찾기 어렵다. 실제로 모의투자 전환
    중에 겪은 문제다.
    """
    import dataclasses

    calls = []
    manager = TokenManager(settings, make_http(settings, calls))
    assert manager.get_token() == "test-token"
    assert len(calls) == 1

    manager.get_token()
    assert len(calls) == 1  # 같은 키면 캐시 재사용

    rekeyed = dataclasses.replace(settings, app_key="a-different-app-key")
    TokenManager(rekeyed, make_http(rekeyed, calls)).get_token()
    assert len(calls) == 2  # 키가 바뀌면 재발급


def test_approval_key_cache_is_invalidated_when_the_app_key_changes(settings):
    import dataclasses

    calls = []

    def http_for(cfg):
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"approval_key": "approval-123"})

        return httpx.Client(base_url=cfg.base_url, transport=httpx.MockTransport(handler))

    ApprovalKeyManager(settings, http_for(settings)).get_key()
    ApprovalKeyManager(settings, http_for(settings)).get_key()
    assert len(calls) == 1

    rekeyed = dataclasses.replace(settings, app_key="a-different-app-key")
    ApprovalKeyManager(rekeyed, http_for(rekeyed)).get_key()
    assert len(calls) == 2

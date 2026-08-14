import json

import httpx

from sontrader.auth import ApprovalKeyManager, TokenManager
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

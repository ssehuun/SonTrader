import pytest

from sontrader.config import Settings

TOKEN_RESPONSE = {
    "access_token": "test-token",
    "token_type": "Bearer",
    "expires_in": 86400,
    "access_token_token_expired": "2099-01-01 00:00:00",
}


@pytest.fixture
def settings(tmp_path):
    return Settings(
        app_key="key",
        app_secret="secret",
        cano="12345678",
        acnt_prdt_cd="01",
        paper=True,
        token_cache=tmp_path / "token.json",
    )

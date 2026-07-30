import pytest

from sontrader.config import PAPER_BASE_URL, REAL_BASE_URL, load_settings


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("KIS_APP_KEY", "key")
    monkeypatch.setenv("KIS_APP_SECRET", "secret")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678-01")
    monkeypatch.setenv("SONTRADER_TOKEN_CACHE", str(tmp_path / "token.json"))
    monkeypatch.delenv("KIS_PAPER", raising=False)
    return monkeypatch


def test_load_settings_parses_account_number(env):
    settings = load_settings()
    assert settings.cano == "12345678"
    assert settings.acnt_prdt_cd == "01"


def test_paper_is_the_default(env):
    settings = load_settings()
    assert settings.paper is True
    assert settings.base_url == PAPER_BASE_URL


def test_real_trading_requires_explicit_opt_out(env):
    env.setenv("KIS_PAPER", "false")
    settings = load_settings()
    assert settings.paper is False
    assert settings.base_url == REAL_BASE_URL


def test_missing_credentials_raise(env):
    env.delenv("KIS_APP_KEY")
    with pytest.raises(RuntimeError, match="KIS_APP_KEY"):
        load_settings()


def test_malformed_account_number_raises(env):
    env.setenv("KIS_ACCOUNT_NO", "1234-5")
    with pytest.raises(RuntimeError, match="KIS_ACCOUNT_NO"):
        load_settings()

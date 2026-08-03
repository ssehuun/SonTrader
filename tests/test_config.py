import pytest

from sontrader.config import PAPER_BASE_URL, REAL_BASE_URL, load_database_url, load_settings


@pytest.fixture
def env(monkeypatch, tmp_path):
    # 개발 머신의 실제 .env가 테스트에 새어 들어오지 않도록 dotenv를 무력화한다.
    monkeypatch.setattr("sontrader.config.load_dotenv", lambda: None)
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


def test_token_cache_tilde_is_expanded(env):
    # "~"가 그대로 남으면 리포지토리 안에 '~' 디렉토리가 생겨 토큰이 저장소에 노출된다.
    env.setenv("SONTRADER_TOKEN_CACHE", "~/danger/token.json")
    settings = load_settings()
    assert "~" not in str(settings.token_cache)
    assert str(settings.token_cache).startswith("/")


def test_database_url_defaults_to_none(env):
    env.delenv("DATABASE_URL", raising=False)
    for name in ("USER", "PASSWORD", "HOST", "PORT", "DB"):
        env.delenv(f"POSTGRES_{name}", raising=False)
    assert load_database_url() is None


def test_database_url_is_read_from_env(env):
    env.setenv("DATABASE_URL", "postgresql+psycopg2://u:p@localhost:5432/trading")
    assert load_database_url() == "postgresql+psycopg2://u:p@localhost:5432/trading"


def test_database_url_composed_from_postgres_vars_escapes_credentials(env):
    env.delenv("DATABASE_URL", raising=False)
    env.setenv("POSTGRES_USER", "trader")
    env.setenv("POSTGRES_PASSWORD", "p@ss:w/rd")  # 특수문자가 그대로 들어가면 URL이 깨진다
    env.setenv("POSTGRES_HOST", "localhost")
    env.setenv("POSTGRES_PORT", "5432")
    env.setenv("POSTGRES_DB", "kis_trading")

    assert (
        load_database_url()
        == "postgresql+psycopg2://trader:p%40ss%3Aw%2Frd@localhost:5432/kis_trading"
    )


def test_partial_postgres_vars_yield_none(env):
    env.delenv("DATABASE_URL", raising=False)
    for name in ("USER", "PASSWORD", "HOST", "PORT", "DB"):
        env.delenv(f"POSTGRES_{name}", raising=False)
    env.setenv("POSTGRES_USER", "trader")

    assert load_database_url() is None

import pytest

from sontrader.config import (
    PAPER_BASE_URL,
    REAL_BASE_URL,
    load_database_url,
    load_entry_trigger,
    load_settings,
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    # 개발 머신의 실제 .env가 테스트에 새어 들어오지 않도록 dotenv를 무력화한다.
    monkeypatch.setattr("sontrader.config.load_dotenv", lambda: None)
    monkeypatch.setenv("KIS_APP_KEY", "key")
    monkeypatch.setenv("KIS_APP_SECRET", "secret")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678-01")
    monkeypatch.setenv("SONTRADER_TOKEN_CACHE", str(tmp_path / "token.json"))
    monkeypatch.delenv("KIS_PAPER", raising=False)
    for name in ("KIS_APP_PAPER_KEY", "KIS_APP_PAPER_SECRET", "KIS_ACCOUNT_PAPER_NO"):
        monkeypatch.delenv(name, raising=False)
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


def _no_dotenv(monkeypatch):
    monkeypatch.setattr("sontrader.config.load_dotenv", lambda: None)


def test_entry_trigger_defaults_to_watchlist(monkeypatch):
    _no_dotenv(monkeypatch)
    """LLM 없이도 신규 진입이 돌아가야 한다 — 기본값이 그 요건을 만족한다."""
    monkeypatch.delenv("SONTRADER_ENTRY_TRIGGER", raising=False)
    assert load_entry_trigger() == "watchlist"


def test_entry_trigger_accepts_event(monkeypatch):
    _no_dotenv(monkeypatch)
    monkeypatch.setenv("SONTRADER_ENTRY_TRIGGER", "EVENT")
    assert load_entry_trigger() == "event"


def test_entry_trigger_rejects_unknown_values(monkeypatch):
    _no_dotenv(monkeypatch)
    """오타를 조용히 기본값으로 대체하면 전략이 말없이 바뀐다 — fail-closed."""
    monkeypatch.setenv("SONTRADER_ENTRY_TRIGGER", "momentum")
    with pytest.raises(RuntimeError, match="SONTRADER_ENTRY_TRIGGER"):
        load_entry_trigger()


# --- 환경별 자격증명 선택 ---------------------------------------------------------
#
# 앱키는 환경별로 발급된다. 실전 키를 모의 도메인에 쓰면 시세조회는 통과하고
# 계좌 API만 EGW02007로 막혀서, 기동한 뒤 한참 지나 드러난다.


def test_paper_credentials_win_in_paper_mode(env):
    env.setenv("KIS_APP_PAPER_KEY", "paper-key")
    env.setenv("KIS_APP_PAPER_SECRET", "paper-secret")
    env.setenv("KIS_ACCOUNT_PAPER_NO", "87654321-02")

    settings = load_settings()

    assert settings.paper is True
    assert settings.app_key == "paper-key"
    assert settings.app_secret == "paper-secret"
    assert settings.cano == "87654321"


def test_paper_credentials_are_ignored_in_real_mode(env):
    """실전 도메인에 모의 키가 붙으면 주문이 통째로 막힌다."""
    env.setenv("KIS_PAPER", "false")
    env.setenv("KIS_APP_PAPER_KEY", "paper-key")
    env.setenv("KIS_APP_PAPER_SECRET", "paper-secret")
    env.setenv("KIS_ACCOUNT_PAPER_NO", "87654321-02")

    settings = load_settings()

    assert settings.paper is False
    assert settings.app_key == "key"
    assert settings.cano == "12345678"


def test_paper_mode_falls_back_to_unsuffixed_names(env):
    """한 벌만 쓰던 기존 .env가 그대로 동작해야 한다."""
    settings = load_settings()

    assert settings.paper is True
    assert settings.app_key == "key"


def test_error_names_the_variable_that_is_actually_used(env):
    """두 이름 중 무엇을 고쳐야 하는지 메시지가 짚어줘야 한다."""
    env.setenv("KIS_ACCOUNT_PAPER_NO", "1234-5")

    with pytest.raises(RuntimeError, match="KIS_ACCOUNT_PAPER_NO"):
        load_settings()

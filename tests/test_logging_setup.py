"""logging_setup.py 테스트 (01문서 §6.6.2, §6.6.3).

가장 중요하게 보는 것은 마스킹이다. 계좌번호가 트레이스백으로 새어 나간
사고가 실제로 있었고(§6.6.3), 그 경로가 바로 여기서 막혀야 한다.
"""

import logging

import pytest

from sontrader.logging_setup import REDACTED, collect_env_secrets, configure, mask

SECRETS = ("PSxxxxxxxxAPPKEY", "12345678")


@pytest.fixture
def capture(monkeypatch, capsys):
    """configure()로 실제 핸들러를 걸고 stdout을 돌려주는 로거."""

    def _run(emit, **env):
        for name in ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO", "DATABASE_URL"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        configure("DEBUG")
        emit(logging.getLogger("sontrader.test"))
        return capsys.readouterr().out

    yield _run
    logging.getLogger().handlers.clear()


# --- 값을 아는 비밀 (환경변수) -----------------------------------------------------


def test_app_key_from_env_is_masked(capture):
    out = capture(
        lambda log: log.info("요청 실패: appkey=PSxxxxxxxxAPPKEY"),
        KIS_APP_KEY="PSxxxxxxxxAPPKEY",
    )

    assert "PSxxxxxxxxAPPKEY" not in out
    assert REDACTED in out


def test_account_number_in_a_url_is_masked(capture):
    """사고 재현 경로 — 계좌번호가 URL 쿼리에 박혀 나갔다."""
    out = capture(
        lambda log: log.error("GET /inquire-balance?CANO=12345678&ACNT_PRDT_CD=01"),
        KIS_ACCOUNT_NO="12345678-01",
    )

    assert "12345678" not in out
    assert "ACNT_PRDT_CD=01" in out  # 상품코드는 비밀이 아니다


def test_secrets_are_masked_inside_tracebacks(capture):
    """필터가 아니라 포매터로 거는 이유. 사고 경로가 트레이스백이었다."""

    def emit(log):
        try:
            raise RuntimeError("token=PSxxxxxxxxAPPKEY rejected")
        except RuntimeError:
            log.exception("주문 실패")

    out = capture(emit, KIS_APP_KEY="PSxxxxxxxxAPPKEY")

    assert "Traceback" in out
    assert "PSxxxxxxxxAPPKEY" not in out


def test_short_env_values_are_not_registered(monkeypatch):
    """짧은 값을 등록하면 무관한 텍스트까지 뭉갠다."""
    monkeypatch.setenv("KIS_APP_KEY", "ab")

    assert "ab" not in collect_env_secrets()


# --- 값을 모르는 비밀 (패턴) -------------------------------------------------------


def test_runtime_access_token_is_masked_without_registration():
    """접근토큰은 런타임 발급이라 환경변수로 미리 알 수 없다."""
    text = 'response: {"access_token": "eyJhbGciOi.SOMETOKEN", "expires_in": 86400}'

    masked = mask(text)

    assert "eyJhbGciOi.SOMETOKEN" not in masked
    assert "expires_in" in masked


def test_bearer_header_is_masked():
    assert "SOMETOKEN" not in mask("Authorization: Bearer SOMETOKEN123")


def test_database_url_password_is_masked():
    """DATABASE_URL은 출력 금지 대상이다 — 비밀번호가 박혀 있다."""
    masked = mask("postgresql+psycopg2://trader:sup3rs3cret@db.example.com:5432/sontrader")

    assert "sup3rs3cret" not in masked
    assert "db.example.com:5432/sontrader" in masked  # 호스트는 진단에 필요하다


def test_plain_text_is_left_alone():
    text = "005930 진입 200주 @ 71,000원"

    assert mask(text) == text


# --- 핸들러 구성 -----------------------------------------------------------------


def test_configure_is_idempotent(monkeypatch):
    """재기동·재호출로 핸들러가 쌓이면 같은 줄이 여러 번 찍힌다."""
    configure("INFO")
    configure("INFO")

    assert len(logging.getLogger().handlers) == 1
    logging.getLogger().handlers.clear()


def test_log_level_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("SONTRADER_LOG_LEVEL", "warning")

    configure()

    assert logging.getLogger().level == logging.WARNING
    logging.getLogger().handlers.clear()

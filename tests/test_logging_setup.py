"""logging_setup.py 테스트 (01문서 §6.6.2, §6.6.3).

가장 중요하게 보는 것은 마스킹이다. 계좌번호가 트레이스백으로 새어 나간
사고가 실제로 있었고(§6.6.3), 그 경로가 바로 여기서 막혀야 한다.
"""

import logging
import re
from datetime import datetime

import pytest

from sontrader.logging_setup import REDACTED, collect_env_secrets, configure, mask, traced
from sontrader.timeutil import now_kst

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


# --- @traced -------------------------------------------------------------------


@pytest.fixture
def at_level(monkeypatch, capsys):
    """지정한 레벨로 configure()한 뒤 stdout을 돌려준다."""

    def _run(level, emit):
        for name in ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO", "DATABASE_URL"):
            monkeypatch.delenv(name, raising=False)
        configure(level)
        emit()
        return capsys.readouterr().out

    yield _run
    logging.getLogger().handlers.clear()


def test_traced_logs_failures_even_at_info_level(at_level):
    """실제로 겪은 사고의 회귀 테스트. 예전 구현은 DEBUG가 아니면 try에
    들어가기도 전에 조기 반환해서, docstring이 약속한 실패 로그가 기본
    레벨에서 영원히 찍히지 않았다 — 관측용 데코레이터가 실패를 감췄다."""

    @traced
    def submit(order):
        raise RuntimeError("KIS 응답 없음")

    def emit():
        with pytest.raises(RuntimeError):
            submit("005930")

    out = at_level("INFO", emit)

    assert "ERROR" in out
    assert "submit 실패" in out
    assert "KIS 응답 없음" in out


def test_traced_reraises_the_original_exception(at_level):
    """로그를 남기되 삼키지 않는다 — 삼키면 호출자가 실패를 모른다."""

    @traced
    def boom():
        raise ValueError("원본")

    def emit():
        with pytest.raises(ValueError, match="원본"):
            boom()

    at_level("INFO", emit)


def test_traced_is_silent_at_info_when_the_call_succeeds(at_level):
    """평상시에는 조용해야 한다. 경계 함수마다 매 사이클 두 줄이 늘면
    하트비트가 묻힌다."""

    @traced
    def cash():
        return 100

    out = at_level("INFO", lambda: cash())

    assert out == ""


def test_traced_logs_enter_and_exit_with_duration_at_debug(at_level):
    @traced
    def cash():
        return 9_494_652

    out = at_level("DEBUG", lambda: cash())

    # qualname이므로 중첩 함수는 "...<locals>.cash"로 찍힌다.
    assert "cash()" in out  # 진입
    assert "ms →" in out  # 이탈 + 소요시간
    assert "9494652" in out  # 반환값
    assert out.count("\n") == 2  # 진입/이탈 두 줄


def test_traced_hides_self_but_keeps_real_arguments(at_level):
    """self는 매번 같아 정보가 없으니 빼고, 실제 인자는 남긴다. 판정을
    데코레이션 시점 시그니처로 하는 이유 — 호출 시점 추측은 첫 인자의 타입이
    우연히 같은 이름의 속성을 가지면 실제 인자를 조용히 삼킨다."""

    class Broker:
        def submit(self, symbol):  # 같은 이름의 속성을 가진 타입
            return "ok"

        submit = traced(submit)

    out = at_level("DEBUG", lambda: Broker().submit("005930"))

    assert "'005930'" in out, "실제 인자가 남아야 한다"
    assert "Broker object" not in out, "self는 빠져야 한다"


def test_traced_truncates_huge_arguments(at_level):
    """봉 300개짜리 리스트가 그대로 펼쳐지면 한 줄이 수만 자가 된다."""

    @traced
    def collect(symbols):
        return symbols

    out = at_level("DEBUG", lambda: collect([f"{i:06d}" for i in range(500)]))

    assert "…" in out
    assert max(len(line) for line in out.splitlines()) < 400


# --- 시각 ----------------------------------------------------------------------


def test_timestamps_are_kst_with_milliseconds(at_level):
    """머신 타임존은 UTC인데 DB는 naive KST다. 시각을 대조할 수 없으면
    "그때 무슨 일이 있었나"를 추적할 수 없다. 밀리초는 한 사이클 안의 순서를
    복원하는 데 필요하다."""
    out = at_level("INFO", lambda: logging.getLogger("sontrader.test").info("x"))

    stamp = out.split(" KST ")[0]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}", stamp), stamp
    logged = datetime.strptime(stamp.split(",")[0], "%Y-%m-%d %H:%M:%S")
    assert abs((logged - now_kst()).total_seconds()) < 5

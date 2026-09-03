"""Bash 명령 가드 훅 (`scripts/bash_guard.py`).

🔴 **가드는 fail-open이라 깨져도 아무 일이 안 일어난다.** 조용히 사라지고,
다음에 시크릿을 읽거나 창을 바꿔도 아무도 안 막는다. 이 테스트가 그걸 잡는
유일한 장치다.

막아야 할 것뿐 아니라 **통과시켜야 할 것도 고정한다** — 멀쩡한 명령에
프롬프트가 뜨면 사람이 읽지 않게 되고, 그게 가드가 죽는 가장 흔한 방식이다.
"""

import importlib.util
import io
import json
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bash_guard.py"
_spec = importlib.util.spec_from_file_location("bash_guard", _PATH)
assert _spec and _spec.loader
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

OK_INDEX = "uv run sontrader index-trend --code 2001 --start 19920103 --end 20260828"
REAL = "KIS_PAPER=false\n"
PAPER = "KIS_PAPER=true\n"


# --- 검사 1 · 시크릿 ----------------------------------------------------------


def test_reading_secret_files_is_blocked():
    """🔴 2026-08-20에 실제로 유출돼 앱키를 재발급했다 (T3)."""
    for command in (
        "cat .env",
        "grep KIS_APP_SECRET .env",
        "head -5 .env",
        "sed -n '1,10p' .env",
        "cat ~/.cache/sontrader/token-real.json",
    ):
        assert guard.check_secrets(command) is not None, command


def test_dumping_secret_env_vars_is_blocked():
    assert guard.check_secrets('echo "$KIS_APP_SECRET"') is not None
    assert guard.check_secrets("echo $DATABASE_URL") is not None
    assert guard.check_secrets("env | grep KIS") is not None


def test_name_only_inspection_passes():
    """값을 안 흘리는 형태는 통과한다 — 막으면 상태 확인이 불가능해진다."""
    assert guard.check_secrets("grep -oE '^KIS_[A-Z_]+=' .env") is None
    assert guard.check_secrets("ls -la .env") is None
    assert guard.check_secrets("grep -c KIS .env") is None


def test_ordinary_commands_pass():
    """훅이 모든 Bash 호출에 붙으므로 무관한 명령은 반드시 조용해야 한다."""
    for command in ("git status", "uv run pytest", "cat README.md", ""):
        assert guard.check_secrets(command) is None, command


# --- 검사 2 · 실전 도메인 ------------------------------------------------------


def test_ordering_on_real_domain_is_denied():
    """주문 경로는 확인이 아니라 차단이다 — Claude가 낼 주문이 아니다."""
    verdict = guard.check_live_domain("uv run python -m sontrader.apps.live", REAL)

    assert verdict is not None
    assert verdict[0] == "deny"


def test_reads_on_real_domain_ask():
    """조회까지 막으면 백필이 멈춘다. 현황이 의도적으로 켜 뒀다고 적어 뒀다."""
    verdict = guard.check_live_domain("uv run sontrader balance", REAL)

    assert verdict is not None
    assert verdict[0] == "ask"


def test_paper_domain_is_silent():
    """모의면 아무것도 안 묻는다 — 기본 상태에서 잡음이 없어야 한다."""
    assert guard.check_live_domain("uv run sontrader balance", PAPER) is None
    assert guard.check_live_domain("uv run python -m sontrader.apps.live", PAPER) is None


def test_paper_flag_is_read_like_config_does():
    """`config.py:182`와 같은 규칙. 다르게 읽으면 거짓 경보가 난다."""
    assert guard._is_real_domain("KIS_PAPER=false\n") is True
    assert guard._is_real_domain("KIS_PAPER=no\n") is True
    assert guard._is_real_domain("KIS_PAPER=true\n") is False
    assert guard._is_real_domain("") is False  # 파일 없음 → 거짓 경보 금지


# --- 검사 3 · 측정 창 ----------------------------------------------------------


def test_registered_window_passes():
    """등록창과 부가 옵션(2단계·비용배수)은 통과한다 — 창을 안 바꾼다."""
    assert guard.check_backtest_window(OK_INDEX) is None
    assert guard.check_backtest_window(f"{OK_INDEX} --n-in 80 --cost-multiple 2") is None
    assert guard.check_backtest_window("sontrader backtest --start 20190114 --end 20231231") is None


def test_extended_end_date_is_flagged():
    """🔴 종료일 연장이 가장 위험하다 — 2026-06 낙폭이 미회복이라 종료일이
    판정을 정한다. 결과를 보고 "9월까지"가 되면 안 된다."""
    reason = guard.check_backtest_window("sontrader index-trend --start 19920103 --end 20260930")

    assert reason is not None
    assert "20260930" in reason  # 요청값
    assert "19920103 ~ 20260828" in reason  # 등록값
    assert "§5.1b" in reason  # 근거 절 — 프롬프트만 보고 판단할 수 있어야 한다


def test_loop_form_is_still_checked():
    """🔴 구멍 없음 회귀.

    `for n in ...; do uv run ...; done`은 `uv run`으로 시작하지 않는다. 훅에
    `if: "Bash(uv run *)"` 필터를 걸었다면 이 형태가 통째로 빠져나갔을 것이다 —
    이 세션에서 실제로 쓴 형태다.
    """
    command = (
        "for n in 50 80; do uv run sontrader index-trend "
        "--start 20140101 --end 20260828 --n-in $n; done"
    )
    assert guard.check_backtest_window(command) is not None


# --- 합성과 훅 계약 ------------------------------------------------------------


def test_strictest_check_wins():
    """시크릿이 다른 둘을 이긴다 — 값이 새는 것이 가장 크다."""
    command = f"cat .env && {OK_INDEX}"

    decision, _ = guard.evaluate(command, REAL)

    assert decision == "deny"


def test_clean_command_yields_no_verdict():
    assert guard.evaluate(OK_INDEX, PAPER) is None


def test_main_emits_decision_and_stays_silent_when_clean(monkeypatch, capsys):
    """훅 계약 — 어긋나면 `permissionDecision`, 통과면 무출력."""
    bad = {"tool_input": {"command": "sontrader index-trend --start 1 --end 2"}}
    monkeypatch.setattr(guard.sys, "stdin", io.StringIO(json.dumps(bad)))
    monkeypatch.setattr(guard, "_read_env", lambda: PAPER)

    assert guard.main() == 0
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "ask"

    good = {"tool_input": {"command": OK_INDEX}}
    monkeypatch.setattr(guard.sys, "stdin", io.StringIO(json.dumps(good)))
    assert guard.main() == 0
    assert capsys.readouterr().out == ""


def test_broken_stdin_does_not_block(monkeypatch, capsys):
    """가드가 깨져도 작업은 계속돼야 한다(fail-open). 그 경로를 고정한다."""
    monkeypatch.setattr(guard.sys, "stdin", io.StringIO("not json"))

    assert guard.main() == 0
    assert capsys.readouterr().out == ""

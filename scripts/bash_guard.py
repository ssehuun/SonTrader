#!/usr/bin/env python3
"""Bash 명령 가드 — 되돌릴 수 없는 것 셋을 실행 **전에** 잡는다.

| # | 검사 | 결정 | 근거 |
|---|---|---|---|
| 1 | `.env`·토큰 캐시의 **시크릿 읽기** | **차단** | `01-실전-차단.md` T3 (실제 유출) |
| 2 | **실전 도메인**(`KIS_PAPER=false`)에서 KIS 호출 | 확인/차단 | `00-현황.md` 🔴 |
| 3 | 사전 등록된 **측정 창**과 다른 백테스트 | 확인 | 규약 §0.1 1·2번 |

Claude Code의 `PreToolUse` 훅으로 붙는다(`.claude/settings.json`). stdin으로
도구 호출 JSON을 받고, 판단만 해서 JSON을 stdout으로 돌려준다.

**한 파일에 셋을 모은 이유**: 훅은 **모든 Bash 호출마다** 뜬다. 검사마다
스크립트를 따로 두면 파이썬을 세 번 띄운다.

## 왜 문서로 부족한가

2026-09-01에 실제로 넘어갔다 — "측정 창을 전 구간으로"라는 지시를 S1까지 확대
적용해 사전 등록된 학습/검증 분할을 건너뛰었고, **이평 필터 계열의 검증 구간이
소진됐다.** 규약 §1.6에도 같은 전례가 있다: 규칙을 쓴 당일에 그 규칙을 어겼다.
**규칙을 아는 것과 지키는 것은 다르다.**

## `ask`와 `deny`를 가르는 기준

🔴 **탐색을 막지 않는다. 막는 것은 Claude가 혼자 넘어가는 것뿐이다.**

| | |
|---|---|
| **`ask`** | 사용자가 승인하면 정당한 것 — 다른 창을 보고 싶을 때가 있다 |
| **`deny`** | 승인받을 일이 아닌 것 — 한 번 기록에 남으면 되돌릴 수 없다 |

## 등록창(검사 3)을 왜 여기 적어 두나

**`docs/`는 이 저장소에서 추적하지 않는다**(`.gitignore`). 새로 클론하면 문서가
없으므로 문서에서 읽을 수 없다. 그래서 값을 아래 표에 사본으로 둔다.

🔴 **문서와 여기가 갈리면 문서가 옳다.** 규약 §5.1·§5.1b를 고칠 때 이 표도
같이 고친다. 사본이 둘이라는 것 자체가 약점이고, `docs/`를 추적하지 않는 한
피할 수 없다.

## 실패하면 통과시킨다 (fail-open)

이 스크립트가 죽어서 작업이 멈추면 안 된다. 대신 **가드가 조용히 사라지는
것을 `tests/test_bash_guard.py`가 잡는다** — 테스트가 유일한 방어다.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

# (시작일, 종료일, 근거) — YYYYMMDD, CLI 인자 그대로.
#
# 🔴 규약 §5.1·§5.1b의 사본이다. 문서를 고치면 여기도 고친다.
REGISTERED: dict[str, list[tuple[str, str, str]]] = {
    "index-trend": [
        ("19920103", "20260828", "전 구간 (규약 §5.1b)"),
    ],
    "backtest": [
        ("20190114", "20231231", "학습 구간 (규약 §5.1)"),
        ("20240101", "20260818", "검증 구간 (규약 §5.1)"),
        # §3 벤치마크가 이 창이라 정당한 실행이다. 빼면 프롬프트가 잡음이 된다.
        ("20190114", "20260818", "전 구간 벤치마크 (규약 §3)"),
    ],
}

# S0 기준선이 KOSPI200이다. 다른 지수를 붙이는 것은 비교 기준을 바꾸는 것이라
# §0.1의 7번에 걸린다 — 새 등록이지 이 창의 변형이 아니다.
INDEX_CODE = "2001"

_TERMINAL_END = (
    "종료일 20260828을 늘리지 않는다 — 2026-06 낙폭(−40.9%)이 미회복이라 종료일이 판정을 정한다."
)


# --- 검사 1 · 시크릿 -----------------------------------------------------------
#
# 🔴 가정이 아니라 **실제로 난 사고**다 (`01-실전-차단.md` T3): 2026-08-20 대화
# 기록에 실전 앱키·시크릿·계좌번호가 출력돼 KIS Developers에서 재발급했다.
# `.env`에 실전/모의 두 벌이 들어 있는 것을 확인하지 않고 grep한 탓이다.
#
# T3가 "완료"인 이유는 `.env` 구조를 고쳤기 때문이지 **다시 못 읽게 막았기
# 때문이 아니다.** 같은 실수를 다시 할 수 있다.

# 값이 들어 있는 파일. 토큰 캐시 경로는 `config.py`의 `_CACHE_DIR`.
_SECRET_FILES = re.compile(r"(^|[\s/'\"=])\.env\b|\.cache/sontrader/(token|approval_key)-")

# 값을 그대로 찍어내는 형태.
_SECRET_DUMP = re.compile(
    r"\$\{?(KIS_[A-Z_]+|DATABASE_URL|TELEGRAM_[A-Z_]+)\b"
    r"|\b(printenv|env)\b[^|]*\|[^|]*(KIS|DATABASE_URL|TELEGRAM)"
    r"|\bprintenv\s+(KIS_|DATABASE_URL|TELEGRAM_)"
)

# 값을 안 흘리는 형태 — 존재·개수·**변수명**만 본다.
# `grep -oE '^KIS_[A-Z_]+='`처럼 등호로 끝나는 패턴은 이름까지만 잡는다.
_SECRET_SAFE = re.compile(
    # `grep -o` + 패턴이 `=`로 끝난다 → 등호까지만 잡으므로 값이 안 나온다
    r"grep\b[^|;]*-[a-zA-Z]*o[a-zA-Z]*\b[^|;]*=['\"]"
    # 개수(-c)·존재(-q)만 본다
    r"|grep\b[^|;]*\s-[a-zA-Z]*[cq]\b"
    # 파일 자체를 안 연다
    r"|^\s*(ls|stat|wc|test|\[)\b"
)

# 파일 내용을 뱉는 도구.
_READERS = re.compile(
    r"\b(cat|bat|less|more|head|tail|grep|rg|ag|sed|awk|xxd|od|strings|nl|cut|dotenv|source)\b"
)


def check_secrets(command: str) -> str | None:
    """`.env`·토큰 캐시의 **값**을 읽으려 하면 차단한다.

    ⚠️ **확인이 아니라 차단이다.** 한 번 대화 기록에 남은 시크릿은 유출로
    취급해야 하고(T3에서 실제로 재발급했다), 승인 프롬프트를 띄우는 시점에는
    이미 명령이 화면에 찍혀 있다. 되돌릴 수 있는 지점은 실행 전뿐이다.
    """
    reads_file = bool(_SECRET_FILES.search(command)) and bool(_READERS.search(command))
    if not reads_file and not _SECRET_DUMP.search(command):
        return None
    if reads_file and _SECRET_SAFE.search(command):
        return None  # 이름·존재·개수만 본다

    return (
        "🔴 시크릿을 읽으려 한다 — 차단됨.\n"
        "  `.env`에는 **실전** 앱키·시크릿·계좌번호가 모의와 함께 들어 있다.\n"
        "  2026-08-20에 같은 방식으로 실전 앱키가 대화 기록에 유출돼 재발급했다\n"
        "  (`docs/system/01-실전-차단.md` T3). 한 번 기록에 남으면 되돌릴 수 없다.\n"
        "  CLAUDE.md 보안 금지사항: 값이 아니라 **변수명·마스킹된 상태만** 본다.\n"
        "  안전한 형태: grep -oE '^KIS_[A-Z_]+=' .env"
    )


# --- 검사 2 · 실전 도메인 -------------------------------------------------------
#
# `config.py:182`가 `KIS_PAPER`로 실전/모의를 가른다. `false`면 조회도 주문도
# **실전 계좌**를 친다. `.env`가 실제로 `false`이고, 백필을 이어가려고 의도적으로
# 둔 것이라 `00-현황.md`에 🔴로 남아 있다.

_ORDERING = re.compile(r"sontrader\s+live\b|sontrader\.apps\.live\b")

_KIS_READS = re.compile(
    r"sontrader\s+(quote|balance|collect-(dart|master|prices|index|minutes)"
    r"|backfill-prices|build-universe|daytrade-universe)\b"
)


def _is_real_domain(env_text: str) -> bool:
    """`config.py:182`와 **같은 규칙**으로 읽는다 — 다르게 읽으면 거짓 경보다."""
    m = re.search(r"^KIS_PAPER=(.*)$", env_text, re.MULTILINE)
    return m is not None and m.group(1).strip().strip("\"'").lower() in ("false", "0", "no")


def check_live_domain(command: str, env_text: str) -> tuple[str, str] | None:
    """실전 도메인에서 KIS를 칠 때 `(결정, 이유)`. 아니면 None.

    주문 경로는 **차단**, 조회는 **확인**이다 — 조회까지 막으면 백필이 멈추고,
    현황이 그 백필 때문에 실전을 의도적으로 켜 뒀다고 적어 놨다.
    """
    orders, reads = _ORDERING.search(command), _KIS_READS.search(command)
    if not orders and not reads:
        return None
    if not _is_real_domain(env_text):
        return None

    head = "🔴 `.env`가 `KIS_PAPER=false` — **실전 계좌**를 친다.\n"
    if orders:
        return (
            "deny",
            head + "  이 명령은 **실제 주문을 낼 수 있다.** 모의로 돌리려면 `KIS_PAPER=true`.\n"
            "  실전이 정말 의도라면 사용자가 직접 실행한다 — Claude가 낼 주문이 아니다.",
        )
    return (
        "ask",
        head + "  조회·수집이라 주문은 안 나가지만 **실전 계좌·실전 한도**를 쓴다.\n"
        "  `00-현황.md`가 백필 때문에 의도적으로 켜 뒀다고 적어 뒀다 — 맞으면 승인.",
    )


# --- 검사 3 · 측정 창 -----------------------------------------------------------


def _arg(command: str, name: str) -> str | None:
    """`--name 값`과 `--name=값`을 모두 잡는다. 없으면 None."""
    m = re.search(rf"--{name}[=\s]+(\S+)", command)
    return m.group(1) if m else None


def check_backtest_window(command: str) -> str | None:
    """어긋나면 사람이 읽을 이유 문자열, 통과면 None.

    명령 문자열 하나만 본다 — 파일도 DB도 안 건드리므로 테스트가 순수하다.

    ⚠️ **한 명령에 백테스트가 여러 번 들어 있어도 창 값은 하나로 본다**
    (`for n in 50 80; do ... done`). 창을 섞어 부르는 명령은 실제로 안 쓴다.
    """
    subcommand = next((s for s in REGISTERED if re.search(rf"\b{s}\b", command)), None)
    if subcommand is None:
        return None

    windows = REGISTERED[subcommand]
    start, end = _arg(command, "start"), _arg(command, "end")
    registered = "\n".join(f"    {s} ~ {e}   {why}" for s, e, why in windows)

    if start is None or end is None:
        return (
            f"⚠️ `{subcommand}`의 측정 창을 확인할 수 없다 — --start/--end가 안 보인다.\n"
            f"  등록된 창:\n{registered}\n"
            "  규약 §0.1 1번: 측정 기간은 사용자 확인 없이 바꾸지 않는다."
        )

    if subcommand == "index-trend":
        code = _arg(command, "code") or INDEX_CODE
        if code != INDEX_CODE:
            return (
                f"⚠️ 등록된 지수가 아니다 — 요청 `{code}`, 등록 `{INDEX_CODE}`(KOSPI200).\n"
                "  S0 기준선이 KOSPI200이라 다른 지수는 **비교 기준을 바꾸는 것**이다\n"
                "  (규약 §0.1 7번). 새 사전 등록이 필요하다."
            )

    if any(start == s and end == e for s, e, _ in windows):
        return None

    lines = [
        f"⚠️ 등록된 측정 창과 다르다 — `{subcommand}`",
        f"    요청:  {start} ~ {end}",
        "  등록된 창:",
        registered,
        "  규약 §0.1 1·2번: 측정 기간과 학습/검증 분할은 사용자 확인 없이 바꾸지 않는다.",
    ]
    if subcommand == "index-trend":
        lines.append(f"  {_TERMINAL_END}")
        lines.append("  🔴 이평 필터 계열의 검증 구간은 2026-09-01에 이미 소진됐다 (규약 §5.1b).")
    else:
        lines.append("  🔴 검증 구간을 한 번 보면 되돌릴 수 없다 — 그 뒤로는 학습 구간이다.")
    return "\n".join(lines)


def evaluate(command: str, env_text: str) -> tuple[str, str] | None:
    """세 검사를 합성한다. **엄한 쪽이 이긴다** — deny > ask.

    시크릿을 먼저 본다. 나머지 둘이 무엇을 말하든 값이 새는 것이 더 크다.
    """
    if (reason := check_secrets(command)) is not None:
        return "deny", reason
    if (verdict := check_live_domain(command, env_text)) is not None:
        return verdict
    if (reason := check_backtest_window(command)) is not None:
        return "ask", reason
    return None


def _read_env() -> str:
    """`.env`를 읽는다. **내용은 판정에만 쓰고 어디에도 출력하지 않는다.**

    없으면 빈 문자열 — 검사 2가 "실전 아님"으로 보고 지나간다. 파일이 없는
    환경(새 클론·CI)에서 거짓 경보를 내지 않기 위해서다.
    """
    try:
        return (pathlib.Path(__file__).resolve().parents[1] / ".env").read_text()
    except OSError:
        return ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        command = payload.get("tool_input", {}).get("command", "")
        verdict = evaluate(command, _read_env()) if isinstance(command, str) else None
    except Exception:
        # 가드가 깨져도 작업은 계속돼야 한다. 이 경로를 테스트가 지킨다.
        return 0

    if verdict is not None:
        decision, reason = verdict
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": reason,
                }
            },
            sys.stdout,
            ensure_ascii=False,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""시각 규약 테스트 — naive KST 벽시계.

핵심은 회귀 방지다: 맨 `datetime.now()`가 다시 들어오면 서버가 UTC일 때
9시간 어긋난 값이 조용히 흐른다.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from sontrader.auth import TokenManager
from sontrader.timeutil import KST, now_kst
from tests.conftest import TOKEN_RESPONSE

SRC = Path(__file__).resolve().parent.parent / "src" / "sontrader"

# 예외: 규약 자체를 정의하는 곳.
_ALLOWED = {"timeutil.py"}


def _bare_clock_reads(source: str) -> list[int]:
    """인자 없는 `datetime.now()`와 모든 `datetime.utcnow()` 호출의 줄 번호.

    정규식이 아니라 AST로 보는 이유: 주석과 문서화 문자열에 적힌
    "datetime.now()"까지 잡혀서(실제로 이 규약을 설명하는 주석이 걸렸다)
    쓸모없는 실패가 난다. `datetime.now(KST)`처럼 인자가 있으면 정상이다.
    """
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in ("now", "utcnow"):
            continue
        base = func.value
        base_name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", None)
        if base_name != "datetime":
            continue
        if func.attr == "utcnow" or not (node.args or node.keywords):
            found.append(node.lineno)
    return found


def test_no_module_reads_a_bare_datetime_now():
    """실제로 겪은 사고의 회귀 테스트. `auth.py`가 맨 `datetime.now()`(UTC)를
    KIS가 준 KST 만료 시각과 비교해서, 토큰이 만료된 뒤에도 약 9시간 동안
    유효하다고 판단했다. KIS는 `EGW00123`으로 답하는데 원인이 시각 비교라는
    단서가 어디에도 없다."""
    offenders = [
        f"{path.relative_to(SRC)}:{lineno}"
        for path in sorted(SRC.rglob("*.py"))
        if path.name not in _ALLOWED
        for lineno in _bare_clock_reads(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], f"맨 datetime.now() 대신 timeutil.now_kst()를 쓴다: {offenders}"


def test_the_scanner_actually_catches_violations():
    """스캐너가 아무것도 못 잡는 상태로 통과하면 위 테스트가 무의미해진다."""
    assert _bare_clock_reads("import datetime\nx = datetime.now()\n") == [2]
    assert _bare_clock_reads("x = datetime.utcnow()\n") == [1]
    assert _bare_clock_reads("x = datetime.now(KST)\n") == []  # 인자 있으면 정상
    assert _bare_clock_reads("# datetime.now() 를 쓰지 말 것\n") == []  # 주석은 무시


def test_now_kst_is_naive_and_matches_the_kst_wall_clock():
    """머신 타임존에 의존하지 않는다는 것이 요점 — 서버가 UTC든 KST든 같은 값."""
    value = now_kst()

    assert value.tzinfo is None  # DB 저장 규약: naive
    reference = datetime.now(KST).replace(tzinfo=None)
    assert abs((value - reference).total_seconds()) < 5


def _cache_token(settings, expires_at: datetime) -> None:
    from sontrader.auth import _app_key_fingerprint

    settings.token_cache.write_text(
        json.dumps(
            {
                "access_token": "cached-token",
                "expires_at": expires_at.strftime("%Y-%m-%d %H:%M:%S"),
                "base_url": settings.base_url,
                "app_key_fp": _app_key_fingerprint(settings.app_key),
            }
        )
    )


def _manager(settings, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=TOKEN_RESPONSE)

    http = httpx.Client(base_url=settings.base_url, transport=httpx.MockTransport(handler))
    return TokenManager(settings, http)


def test_token_expiry_is_judged_against_kst_not_machine_time(settings):
    """만료 시각을 **KST 기준**으로 이미 지난 값으로 두면 재발급해야 한다.

    이 머신은 UTC라 `datetime.now()`는 KST보다 9시간 이르다. 고치기 전에는
    "아직 9시간 남았다"고 보고 죽은 토큰을 그대로 반환했다.
    """
    _cache_token(settings, now_kst() - timedelta(minutes=1))
    calls: list[httpx.Request] = []
    manager = _manager(settings, calls)

    assert manager.get_token() == "test-token"  # 캐시가 아니라 새 토큰
    assert len(calls) == 1


def test_token_still_inside_its_kst_validity_is_reused(settings):
    """반대 방향도 고정한다 — 유효한 토큰을 매번 재발급하면 KIS가 이전
    토큰을 무효화하고 발급 유량 제한에 걸린다."""
    _cache_token(settings, now_kst() + timedelta(hours=5))
    calls: list[httpx.Request] = []
    manager = _manager(settings, calls)

    assert manager.get_token() == "cached-token"
    assert calls == []


def test_expiry_margin_rejects_a_token_about_to_expire(settings):
    """10분 여유 안에 든 토큰은 미리 재발급한다 — 사이클 중간에 만료되면
    그 사이클의 주문이 실패한다."""
    _cache_token(settings, now_kst() + timedelta(minutes=5))
    calls: list[httpx.Request] = []
    manager = _manager(settings, calls)

    assert manager.get_token() == "test-token"
    assert len(calls) == 1

"""BarView look-ahead 차단 테스트 (구현 계획 5단계).

02문서: "가장 중요한 테스트는 look-ahead와 멱등성 두 가지다. 전자는 백테스트를
거짓으로 만든다." 이 파일이 그 절반을 담당한다 — 미래 봉이 `history()`/
`latest()` 어느 경로로도 새지 않는다는 것을 확인한다.
"""

from datetime import datetime, timedelta

import pytest

from sontrader.core.types import Bar
from sontrader.engine.context import InMemoryBarView

SYMBOL = "005930"
DAY0 = datetime(2026, 3, 1)


def make_bar(symbol: str, day_offset: int, close: int) -> Bar:
    ts = DAY0 + timedelta(days=day_offset)
    return Bar(symbol=symbol, ts=ts, open=close, high=close, low=close, close=close, volume=1_000)


SERIES = [make_bar(SYMBOL, i, 10_000 + i * 100) for i in range(10)]  # day0..day9


# --- look-ahead 차단 -----------------------------------------------------------


def test_latest_never_returns_a_bar_after_now():
    view = InMemoryBarView({SYMBOL: SERIES}).at(DAY0 + timedelta(days=3))

    bar = view.latest(SYMBOL)

    assert bar is not None
    assert bar.ts <= view.now
    assert bar.close == SERIES[3].close


def test_history_never_leaks_a_future_bar_even_when_count_is_huge():
    view = InMemoryBarView({SYMBOL: SERIES}).at(DAY0 + timedelta(days=3))

    bars = view.history(SYMBOL, count=1_000)

    assert all(b.ts <= view.now for b in bars)
    assert bars == SERIES[:4]  # day0..day3 포함, day4 이후는 없음


def test_bar_exactly_at_now_is_visible():
    view = InMemoryBarView({SYMBOL: SERIES}).at(SERIES[5].ts)

    assert view.latest(SYMBOL) == SERIES[5]


def test_bar_one_moment_after_now_is_invisible():
    view = InMemoryBarView({SYMBOL: SERIES}).at(SERIES[5].ts - timedelta(microseconds=1))

    assert view.latest(SYMBOL) == SERIES[4]


def test_now_before_the_first_bar_sees_nothing():
    view = InMemoryBarView({SYMBOL: SERIES}).at(DAY0 - timedelta(days=1))

    assert view.latest(SYMBOL) is None
    assert view.history(SYMBOL, 10) == []


def test_default_view_without_an_explicit_now_sees_nothing():
    # fail-closed: 시각을 명시하지 않으면 아무것도 보이지 않아야 한다.
    view = InMemoryBarView({SYMBOL: SERIES})

    assert view.latest(SYMBOL) is None
    assert view.history(SYMBOL, 10) == []


def test_at_does_not_mutate_the_original_view():
    original = InMemoryBarView({SYMBOL: SERIES}).at(SERIES[2].ts)

    later = original.at(SERIES[8].ts)

    assert original.latest(SYMBOL) == SERIES[2]  # 원본은 그대로
    assert later.latest(SYMBOL) == SERIES[8]


# --- history() 개수/부족 ------------------------------------------------------


def test_history_returns_the_most_recent_n_bars_in_ascending_order():
    view = InMemoryBarView({SYMBOL: SERIES}).at(SERIES[7].ts)

    bars = view.history(SYMBOL, count=3)

    assert bars == SERIES[5:8]


def test_history_returns_what_is_available_when_count_exceeds_visible_bars():
    view = InMemoryBarView({SYMBOL: SERIES}).at(SERIES[1].ts)

    bars = view.history(SYMBOL, count=100)

    assert bars == SERIES[:2]


def test_history_with_nonpositive_count_returns_empty():
    view = InMemoryBarView({SYMBOL: SERIES}).at(SERIES[5].ts)

    assert view.history(SYMBOL, count=0) == []
    assert view.history(SYMBOL, count=-5) == []


# --- 알 수 없는 종목 / 정렬 -----------------------------------------------------


def test_unknown_symbol_returns_empty_not_an_error():
    view = InMemoryBarView({SYMBOL: SERIES}).at(SERIES[5].ts)

    assert view.latest("000660") is None
    assert view.history("000660", 5) == []


def test_out_of_order_input_is_sorted_before_use():
    shuffled = [SERIES[3], SERIES[0], SERIES[2], SERIES[1]]
    view = InMemoryBarView({SYMBOL: shuffled}).at(SERIES[3].ts)

    assert view.history(SYMBOL, count=10) == SERIES[:4]


def test_bar_symbol_mismatch_is_rejected_at_construction():
    foreign = Bar(symbol="000660", ts=DAY0, open=1, high=1, low=1, close=1, volume=1)
    with pytest.raises(ValueError, match="symbol"):
        InMemoryBarView({SYMBOL: [foreign]})


# --- 여러 종목 ------------------------------------------------------------------


def test_symbols_do_not_leak_into_each_other():
    other = [make_bar("000660", i, 50_000) for i in range(5)]
    view = InMemoryBarView({SYMBOL: SERIES, "000660": other}).at(DAY0 + timedelta(days=2))

    assert view.latest(SYMBOL).symbol == SYMBOL
    assert view.latest("000660").symbol == "000660"

"""청산 규칙 테스트 (구현 계획 4단계).

02 문서 6절이 지목한 두 가지에 집중한다: **구간 경계값**(+4.9% / +5.0%)과
**스톱 하향 금지**. 후자는 단일 시점 공식만으로는 성립하지 않으므로(ATR 확대
시 되돌아간다) 래칫이 실제로 작동하는지 반증 가능한 형태로 확인한다.
"""

from datetime import datetime, timedelta

import pytest

from sontrader.core.exit_rules import (
    BREAKEVEN_TRIGGER,
    ExitReason,
    average_true_range,
    evaluate,
    stop_level,
    trailing_stop,
)
from sontrader.core.types import Bar, ExitRule, Position

SYMBOL = "005930"
ENTRY_TS = datetime(2026, 1, 5, 9, 30)
ENTRY_PRICE = 10_000.0


def make_bar(index: int, close: int, *, high: int | None = None, low: int | None = None) -> Bar:
    return Bar(
        symbol=SYMBOL,
        ts=ENTRY_TS + timedelta(minutes=index),
        open=close,
        high=close if high is None else high,
        low=close if low is None else low,
        close=close,
        volume=1_000,
    )


def make_series(closes: list[int]) -> list[Bar]:
    return [make_bar(i, close) for i, close in enumerate(closes)]


def make_position(rule: ExitRule | None = None, **overrides) -> Position:
    base = dict(
        symbol=SYMBOL,
        qty=10,
        avg_price=ENTRY_PRICE,
        entered_at=ENTRY_TS,
        exit_rule=rule or ExitRule(),
    )
    return Position(**{**base, **overrides})


# --- 구간 경계값 -------------------------------------------------------------


def test_fixed_stop_holds_just_below_the_breakeven_trigger():
    # +4.9% — 아직 고정 손절 구간.
    bars = make_series([10_000, 10_490])
    assert trailing_stop(make_position(), bars) == pytest.approx(9_500.0)


def test_stop_moves_to_breakeven_exactly_at_the_trigger():
    # +5.0% 도달 시점에 본전 이동. ATR 창이 아직 비어 있으므로 진입가에 머문다.
    bars = make_series([10_000, 10_500])
    assert trailing_stop(make_position(), bars) == pytest.approx(ENTRY_PRICE)


def test_stop_level_formula_at_each_regime():
    rule = ExitRule(stop_loss_pct=-0.05, atr_k=2.0)
    below = ENTRY_PRICE * (1 + BREAKEVEN_TRIGGER) - 1
    at = ENTRY_PRICE * (1 + BREAKEVEN_TRIGGER)

    assert stop_level(ENTRY_PRICE, below, 100.0, rule=rule) == pytest.approx(9_500.0)
    # 트리거를 넘는 순간 ATR 트레일링으로 전환된다. 진입가는 고정값이 아니라
    # 바닥선이므로, 변동성이 작으면 스톱은 이미 본전 위에 있다 (10500 - 2*100).
    assert stop_level(ENTRY_PRICE, at, 100.0, rule=rule) == pytest.approx(10_300.0)
    # 12000 - 2*100 = 11800.
    assert stop_level(ENTRY_PRICE, 12_000.0, 100.0, rule=rule) == pytest.approx(11_800.0)
    # 트레일링 값이 진입가 아래로 내려가면 진입가가 바닥이 된다.
    assert stop_level(ENTRY_PRICE, 12_000.0, 2_000.0, rule=rule) == pytest.approx(ENTRY_PRICE)


def test_missing_atr_falls_back_to_breakeven_not_to_the_fixed_stop():
    # 이 구간에 왔다는 건 이미 +5% 이상이라는 뜻. −5%로 되돌리면 스톱 하향이다.
    rule = ExitRule()
    assert stop_level(ENTRY_PRICE, 11_000.0, None, rule=rule) == pytest.approx(ENTRY_PRICE)


# --- 하향 금지 (래칫) --------------------------------------------------------


def volatility_expansion_bars() -> list[Bar]:
    """상승(좁은 범위) 후, 종가는 그대로인데 변동성만 폭발하는 구간."""
    rising = [make_bar(i, 10_000 + 200 * i) for i in range(10)]  # 10000 → 11800, TR=200
    # 종가 11800 고정, 고·저가만 벌어져 TR=4000.
    choppy = [make_bar(10 + i, 11_800, high=13_800, low=9_800) for i in range(5)]
    return rising + choppy


def test_stop_never_ratchets_down_when_volatility_expands():
    rule = ExitRule(atr_period=5, atr_k=2.0)
    position = make_position(rule)
    bars = volatility_expansion_bars()

    levels = [trailing_stop(position, bars[: n + 1]) for n in range(len(bars))]

    assert levels == sorted(levels), levels


def test_ratchet_actually_does_work_the_pointwise_formula_would_not():
    # 반증: 래칫이 없다면 스톱이 11400 → 10000으로 되돌아간다.
    rule = ExitRule(atr_period=5, atr_k=2.0)
    position = make_position(rule)
    bars = volatility_expansion_bars()

    ratcheted = trailing_stop(position, bars)
    atr_now = average_true_range(bars, rule.atr_period)
    pointwise_now = stop_level(ENTRY_PRICE, 11_800.0, atr_now, rule=rule)

    assert ratcheted == pytest.approx(11_400.0)  # 11800 - 2*200, 상승 구간의 최고 스톱
    assert pointwise_now == pytest.approx(ENTRY_PRICE)
    assert pointwise_now < ratcheted


def test_high_water_uses_closes_not_intraday_highs():
    # 위쪽 꼬리(고가 20000)로 high_water를 올리면 스톱이 노이즈로 타이트해진다.
    bars = [make_bar(0, 10_000), make_bar(1, 10_000, high=20_000), make_bar(2, 10_000)]
    assert trailing_stop(make_position(), bars) == pytest.approx(9_500.0)


def test_bars_before_entry_do_not_move_the_stop():
    position = make_position(entered_at=ENTRY_TS + timedelta(minutes=5))
    # 진입 전에 +20% 갔다가 진입 시점에 되돌아온 시계열.
    bars = make_series([12_000] * 5 + [10_000] * 3)

    assert trailing_stop(position, bars) == pytest.approx(9_500.0)


def test_no_bars_yet_means_the_fixed_stop():
    assert trailing_stop(make_position(), []) == pytest.approx(9_500.0)


# --- ATR --------------------------------------------------------------------


def test_average_true_range_is_a_simple_mean_of_true_ranges():
    bars = [
        make_bar(0, 100),
        make_bar(1, 105, high=110, low=90),  # TR = max(20, 10, 10) = 20
        make_bar(2, 110, high=120, low=100),  # TR = max(20, 15, 5) = 20
    ]
    assert average_true_range(bars, 2) == pytest.approx(20.0)


def test_average_true_range_needs_period_plus_one_bars():
    # 첫 봉은 직전 종가가 없어 TR을 계산할 수 없다.
    bars = make_series([100, 105, 110])
    assert average_true_range(bars, 3) is None
    assert average_true_range(bars, 2) is not None
    assert average_true_range([], 5) is None


def test_average_true_range_rejects_bad_period():
    with pytest.raises(ValueError):
        average_true_range(make_series([100, 105]), 0)


# --- evaluate ---------------------------------------------------------------


def test_no_signal_while_price_stays_above_the_stop():
    bars = make_series([10_000, 10_100, 10_050])
    assert evaluate(make_position(), bars, now=ENTRY_TS + timedelta(minutes=3)) is None


def test_stop_fires_on_the_closing_price():
    bars = make_series([10_000, 9_600, 9_499])
    signal = evaluate(make_position(), bars, now=ENTRY_TS + timedelta(minutes=3))

    assert signal is not None
    assert signal.reason is ExitReason.STOP
    assert signal.stop_level == pytest.approx(9_500.0)
    assert signal.trigger_price == 9_499


def test_stop_does_not_fire_on_an_intraday_low_alone():
    # 저가는 9000까지 찔렀지만 종가는 스톱 위. 설계 4절: 판정은 종가 기준.
    bars = [make_bar(0, 10_000), make_bar(1, 9_600, low=9_000)]
    assert evaluate(make_position(), bars, now=ENTRY_TS + timedelta(minutes=2)) is None


def test_trailing_stop_fires_after_the_ratchet_has_moved_up():
    rule = ExitRule(atr_period=5, atr_k=2.0)
    bars = volatility_expansion_bars() + [make_bar(15, 11_000)]
    signal = evaluate(make_position(rule), bars, now=ENTRY_TS + timedelta(minutes=16))

    assert signal is not None
    assert signal.reason is ExitReason.STOP
    assert signal.stop_level == pytest.approx(11_400.0)
    assert signal.trigger_price == 11_000


def test_max_hold_fires_independently_of_the_stop():
    rule = ExitRule(max_hold_days=30)
    bars = make_series([10_000, 10_100])

    held_29 = evaluate(make_position(rule), bars, now=ENTRY_TS + timedelta(days=29))
    held_30 = evaluate(make_position(rule), bars, now=ENTRY_TS + timedelta(days=30))

    assert held_29 is None
    assert held_30 is not None
    assert held_30.reason is ExitReason.MAX_HOLD
    assert held_30.trigger_price == 10_100


def test_max_hold_fires_even_without_bars():
    rule = ExitRule(max_hold_days=1)
    signal = evaluate(make_position(rule), [], now=ENTRY_TS + timedelta(days=2))

    assert signal is not None
    assert signal.reason is ExitReason.MAX_HOLD
    assert signal.trigger_price is None


def test_stop_wins_when_both_conditions_hold():
    rule = ExitRule(max_hold_days=1)
    bars = make_series([10_000, 9_000])
    signal = evaluate(make_position(rule), bars, now=ENTRY_TS + timedelta(days=2))

    assert signal is not None
    assert signal.reason is ExitReason.STOP


def test_bars_of_another_symbol_are_rejected():
    # 조용히 잘못된 스톱을 계산하느니 터지는 편이 낫다.
    foreign = Bar(symbol="000660", ts=ENTRY_TS, open=1, high=1, low=1, close=1, volume=1)
    with pytest.raises(ValueError, match="symbol"):
        evaluate(make_position(), [foreign], now=ENTRY_TS)

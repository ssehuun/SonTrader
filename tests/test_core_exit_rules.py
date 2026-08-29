"""청산 규칙 테스트 (구현 계획 4단계).

02 문서 6절이 지목한 두 가지에 집중한다: **구간 경계값**(+4.9% / +5.0%)과
**스톱 하향 금지**. 후자는 단일 시점 공식만으로는 성립하지 않으므로(ATR 확대
시 되돌아간다) 래칫이 실제로 작동하는지 반증 가능한 형태로 확인한다.
"""

from datetime import datetime, timedelta

import pytest

from sontrader.core.exit_rules import (
    ExitReason,
    average_true_range,
    evaluate,
    stop_level,
    trailing_stop,
)
from sontrader.core.types import Bar, ExitRule, Position, StopBasis

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
    below = ENTRY_PRICE * (1 + rule.breakeven_trigger) - 1
    at = ENTRY_PRICE * (1 + rule.breakeven_trigger)

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


def test_breakeven_trigger_is_injectable_per_rule():
    """문턱을 넓히면 본전 이동이 늦어진다 — 스윕이 실제로 동작하는지."""
    wide = ExitRule(stop_loss_pct=-0.05, atr_k=2.0, breakeven_trigger=0.20)

    # +5%는 넓은 문턱(20%)에 못 미치므로 아직 고정 손절 구간이다.
    assert stop_level(ENTRY_PRICE, ENTRY_PRICE * 1.05, 100.0, rule=wide) == pytest.approx(9_500.0)
    # +20%에 도달해야 트레일링으로 전환된다 (12000 - 2*100).
    assert stop_level(ENTRY_PRICE, ENTRY_PRICE * 1.20, 100.0, rule=wide) == pytest.approx(11_800.0)


def test_exit_rule_round_trip_preserves_breakeven_trigger():
    """포지션 저장/복원에서 살아남아야 한다 — 전역 값이 바뀌어도 과거 스톱이 재현된다."""
    rule = ExitRule(breakeven_trigger=0.12, stop_loss_pct=-0.08)
    assert ExitRule.from_dict(rule.to_dict()) == rule


# --- 스톱 판정 기준 (StopBasis) ---------------------------------------------
#
# 설계 4절의 "종가 기준"은 **분봉** 종가를 전제한 문장이다. 일봉으로 판정하면
# 같은 문구가 전혀 다른 규칙이 된다 — 장중에 스톱을 크게 깨고 종가에 회복한
# 날을 통째로 놓친다. 두 기준이 실제로 갈리는 지점을 고정한다.


def test_close_basis_ignores_an_intrabar_stop_breach():
    """장중 저가가 스톱을 깼지만 종가가 회복하면 CLOSE 기준은 발동하지 않는다.

    이것이 현행 기본값의 정확한 한계다 — 손실 769건 중 525건(68.3%)이
    −5% 스톱보다 나쁘게 청산되던 원인이다(실측 2026-08-26).
    """
    rule = ExitRule(stop_basis=StopBasis.CLOSE)
    position = make_position(rule)
    # 고정 손절 레벨은 10,000 × 0.95 = 9,500.
    bars = [make_bar(0, 10_000), make_bar(1, 9_800, low=9_000)]

    assert evaluate(position, bars, now=ENTRY_TS + timedelta(days=1)) is None


def test_low_basis_fires_on_the_same_intrabar_breach():
    rule = ExitRule(stop_basis=StopBasis.LOW)
    position = make_position(rule)
    bars = [make_bar(0, 10_000), make_bar(1, 9_800, low=9_000)]

    signal = evaluate(position, bars, now=ENTRY_TS + timedelta(days=1))

    assert signal is not None
    assert signal.reason is ExitReason.STOP
    assert signal.trigger_price == 9_000  # 판정 근거는 저가다


def test_both_bases_agree_when_the_close_itself_breaches():
    bars = [make_bar(0, 10_000), make_bar(1, 9_400)]

    for basis in (StopBasis.CLOSE, StopBasis.LOW):
        signal = evaluate(make_position(ExitRule(stop_basis=basis)), bars, now=ENTRY_TS)
        assert signal is not None, basis
        assert signal.reason is ExitReason.STOP


def test_low_basis_does_not_raise_high_water_on_the_upside():
    """아래를 저가로 보더라도 위를 고가로 올리지는 않는다 (확정사항 1).

    high_water가 위쪽 꼬리로 올라가면 스톱이 그만큼 타이트해져, 저가 판정으로
    피하려던 노이즈가 반대 방향으로 그대로 들어온다.
    """
    rule = ExitRule(stop_basis=StopBasis.LOW, atr_period=1)
    position = make_position(rule)
    # 종가는 계속 진입가 근처인데 고가만 크게 튄 봉을 넣는다. high_water가
    # 고가를 따라갔다면 본전 이동(+5%)이 발동해 스톱이 10,000으로 올라간다.
    bars = [make_bar(0, 10_000), make_bar(1, 10_050, high=12_000), make_bar(2, 9_900)]

    assert evaluate(position, bars, now=ENTRY_TS) is None


def test_stop_basis_survives_a_round_trip_through_storage():
    rule = ExitRule(stop_basis=StopBasis.LOW)

    assert ExitRule.from_dict(rule.to_dict()).stop_basis is StopBasis.LOW


def test_an_unknown_stop_basis_is_rejected():
    """조용히 기본값으로 대체하면 그 포지션만 다른 규칙으로 판정된다 (fail-closed)."""
    payload = ExitRule().to_dict() | {"stop_basis": "nonsense"}

    with pytest.raises(ValueError, match="unknown stop basis"):
        ExitRule.from_dict(payload)


def test_old_stored_rules_without_a_stop_basis_default_to_close():
    """이 필드가 생기기 전에 저장된 포지션은 기존 동작을 유지해야 한다."""
    payload = {k: v for k, v in ExitRule().to_dict().items() if k != "stop_basis"}

    assert ExitRule.from_dict(payload).stop_basis is StopBasis.CLOSE


# --- 봉 개수 보유 상한 (T24 선택지 B / R13) -----------------------------------
#
# 달력일로는 "오늘 안에 닫는다"를 표현할 수 없다 — 최소 단위가 1일이고
# `max_hold_days=1`은 "다음 날 첫 사이클", 0은 `__post_init__`이 거부한다.


def test_max_hold_bars_fires_on_bar_count_not_on_the_calendar():
    rule = ExitRule(max_hold_bars=3)
    position = make_position(rule)
    # 진입 봉 포함 3봉째에 발동. 같은 날 안이라 max_hold_days(30)는 걸리지 않는다.
    bars = make_series([10_000, 10_010, 10_020])

    signal = evaluate(position, bars, now=bars[-1].ts)

    assert signal is not None
    assert signal.reason is ExitReason.MAX_HOLD


def test_max_hold_bars_does_not_fire_one_bar_early():
    position = make_position(ExitRule(max_hold_bars=3))
    bars = make_series([10_000, 10_010])

    assert evaluate(position, bars, now=bars[-1].ts) is None


def test_max_hold_bars_is_off_by_default():
    """기본값 None = 스윙 동작 그대로. 기준선이 움직이면 안 된다."""
    assert ExitRule().max_hold_bars is None
    position = make_position()
    bars = make_series([10_000] * 50)

    assert evaluate(position, bars, now=bars[-1].ts) is None


def test_bars_before_entry_are_not_counted_toward_the_hold_cap():
    """ATR 창을 채우려고 진입 이전 봉을 함께 넘긴다 — 그것까지 세면 안 된다."""
    position = make_position(ExitRule(max_hold_bars=3), entered_at=ENTRY_TS + timedelta(minutes=5))
    bars = make_series([10_000] * 6)  # 진입 이후 봉은 1개(index 5)뿐

    assert evaluate(position, bars, now=bars[-1].ts) is None


def test_calendar_and_bar_caps_both_apply_and_the_first_one_wins():
    """둘을 함께 걸 수 있다. 스윙 규칙을 유지한 채 봉 상한만 얹는 경우."""
    position = make_position(ExitRule(max_hold_days=1, max_hold_bars=10_000))
    bars = make_series([10_000, 10_010])

    signal = evaluate(position, bars, now=ENTRY_TS + timedelta(days=1))

    assert signal is not None
    assert signal.reason is ExitReason.MAX_HOLD  # 봉은 2개뿐인데 달력일이 먼저 걸렸다


# --- 세션 종료 청산 (R12) -----------------------------------------------------


def test_eod_fires_when_the_session_is_about_to_end():
    position = make_position(ExitRule(eod_exit_bars=20))
    bars = make_series([10_000, 10_010])

    signal = evaluate(position, bars, now=bars[-1].ts, session_bars_remaining=20)

    assert signal is not None
    assert signal.reason is ExitReason.EOD


def test_eod_does_not_fire_while_the_session_still_has_room():
    position = make_position(ExitRule(eod_exit_bars=20))
    bars = make_series([10_000, 10_010])

    assert evaluate(position, bars, now=bars[-1].ts, session_bars_remaining=21) is None


def test_eod_never_fires_when_the_remaining_bar_count_is_unknown():
    """일봉 재생에는 세션 개념이 없어 늘 None이다. 여기서 발동하면 **매 사이클**
    청산하게 되고 일봉 기준선이 통째로 무너진다."""
    position = make_position(ExitRule(eod_exit_bars=20))
    bars = make_series([10_000, 10_010])

    assert evaluate(position, bars, now=bars[-1].ts, session_bars_remaining=None) is None


def test_a_stop_breach_outranks_the_session_ending():
    """사유 분해(R16)가 의미를 가지려면 순서가 고정돼야 한다."""
    position = make_position(ExitRule(eod_exit_bars=20))
    bars = make_series([10_000, 9_000])  # 고정 손절(-5%) 이탈

    signal = evaluate(position, bars, now=bars[-1].ts, session_bars_remaining=0)

    assert signal is not None
    assert signal.reason is ExitReason.STOP


def test_exit_rule_rejects_nonsense_bar_caps():
    with pytest.raises(ValueError, match="max_hold_bars"):
        ExitRule(max_hold_bars=0)
    with pytest.raises(ValueError, match="eod_exit_bars"):
        ExitRule(eod_exit_bars=-1)


def test_the_new_fields_survive_a_round_trip_through_storage():
    """`positions.exit_rule_json`에 실려 나갔다 돌아와도 같아야 한다 —
    아니면 재시작 후 그 포지션만 다른 규칙으로 판정된다."""
    rule = ExitRule(max_hold_bars=380, eod_exit_bars=20)
    assert ExitRule.from_dict(rule.to_dict()) == rule
    # None도 살아남아야 한다. `int(None)`은 터진다.
    plain = ExitRule()
    assert ExitRule.from_dict(plain.to_dict()) == plain
    # 예전에 저장된(이 필드가 없는) 레코드도 읽혀야 한다.
    legacy = {"technical": "atr_trailing", "max_hold_days": 30}
    assert ExitRule.from_dict(legacy).max_hold_bars is None

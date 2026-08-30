"""성과 지표 테스트 (01문서 §5.3 — apps/report.py).

`BacktestResult`를 직접 손으로 구성해 각 지표를 손계산값과 대조한다.
`replay()`를 다시 돌리지 않는다 — 시뮬레이션 로직은 test_apps_backtest.py가
이미 검증했고, 여기서 볼 것은 순수 계산식이다.
"""

from datetime import date, datetime, timedelta

import pytest

from sontrader.apps.backtest import BacktestResult, ClosedTrade
from sontrader.apps.report import MIN_TRADE_SAMPLE, build_report


def make_result(*, equity_curve=(), closed_trades=(), total_costs=0) -> BacktestResult:
    return BacktestResult(
        equity_curve=tuple(equity_curve),
        fills=(),
        rejections=(),
        closed_trades=tuple(closed_trades),
        total_costs=total_costs,
        final_cash=0,
        final_positions=(),
    )


def make_trade(symbol, entered_at, exit_at, entry_price, exit_price, qty=100) -> ClosedTrade:
    return ClosedTrade(
        symbol=symbol,
        entered_at=entered_at,
        exit_at=exit_at,
        entry_price=entry_price,
        exit_price=exit_price,
        qty=qty,
    )


# --- CAGR ---------------------------------------------------------------------


def test_cagr_over_one_year():
    curve = [(date(2025, 1, 1), 10_000_000), (date(2026, 1, 1), 12_000_000)]
    report = build_report(make_result(equity_curve=curve), initial_cash=10_000_000)
    assert report.cagr == pytest.approx(0.2)


def test_cagr_over_two_years():
    curve = [(date(2025, 1, 1), 10_000_000), (date(2027, 1, 1), 14_400_000)]
    report = build_report(make_result(equity_curve=curve), initial_cash=10_000_000)
    assert report.cagr == pytest.approx(0.2)


def test_cagr_is_none_for_a_single_point_curve():
    curve = [(date(2025, 1, 1), 10_000_000)]
    report = build_report(make_result(equity_curve=curve), initial_cash=10_000_000)
    assert report.cagr is None


def test_cagr_is_none_for_an_empty_curve():
    report = build_report(make_result(), initial_cash=10_000_000)
    assert report.cagr is None


def test_cagr_is_none_when_final_equity_is_not_positive():
    curve = [(date(2025, 1, 1), 10_000_000), (date(2026, 1, 1), 0)]
    report = build_report(make_result(equity_curve=curve), initial_cash=10_000_000)
    assert report.cagr is None


# --- MDD ------------------------------------------------------------------------


def test_mdd_finds_the_worst_peak_to_trough_drawdown():
    dates = [date(2026, 1, i) for i in range(1, 7)]
    values = [100, 120, 90, 110, 80, 130]
    curve = list(zip(dates, values, strict=True))
    report = build_report(make_result(equity_curve=curve), initial_cash=100)
    assert report.mdd == pytest.approx(1 / 3)


def test_mdd_uses_initial_cash_as_the_starting_peak():
    curve = [(date(2026, 1, 1), 80), (date(2026, 1, 2), 90)]
    report = build_report(make_result(equity_curve=curve), initial_cash=100)
    assert report.mdd == pytest.approx(0.2)  # 초기자본 100 대비 80


def test_mdd_is_zero_for_an_empty_curve():
    report = build_report(make_result(), initial_cash=10_000_000)
    assert report.mdd == 0.0


# --- 샤프 --------------------------------------------------------------------


def test_sharpe_is_none_with_fewer_than_two_returns():
    curve = [(date(2026, 1, 1), 100), (date(2026, 1, 2), 110)]
    report = build_report(make_result(equity_curve=curve), initial_cash=100)
    assert report.sharpe is None


def test_sharpe_is_none_when_returns_have_zero_variance():
    curve = [(date(2026, 1, i), v) for i, v in enumerate([100, 110, 121], start=1)]
    report = build_report(make_result(equity_curve=curve), initial_cash=100)
    assert report.sharpe is None  # 매번 정확히 +10% — 분산 0


def test_sharpe_is_positive_for_a_generally_rising_curve():
    curve = [(date(2026, 1, i), v) for i, v in enumerate([100, 105, 103, 108, 110], start=1)]
    report = build_report(make_result(equity_curve=curve), initial_cash=100)
    assert report.sharpe is not None
    assert report.sharpe > 0


def test_sharpe_is_negative_for_a_generally_falling_curve():
    curve = [(date(2026, 1, i), v) for i, v in enumerate([100, 95, 97, 90, 88], start=1)]
    report = build_report(make_result(equity_curve=curve), initial_cash=100)
    assert report.sharpe is not None
    assert report.sharpe < 0


# --- 승률 / 손익비 --------------------------------------------------------------


def test_win_rate_and_profit_factor_from_mixed_trades():
    entered = datetime(2026, 1, 1)
    trades = [
        make_trade(
            "100", entered, entered + timedelta(days=1), 10_000, 11_000, qty=100
        ),  # +100,000
        make_trade("200", entered, entered + timedelta(days=1), 10_000, 9_000, qty=100),  # -100,000
        make_trade(
            "300", entered, entered + timedelta(days=1), 10_000, 12_000, qty=100
        ),  # +200,000
    ]
    report = build_report(make_result(closed_trades=trades), initial_cash=10_000_000)

    assert report.win_rate == pytest.approx(2 / 3)
    assert report.profit_factor == pytest.approx(3.0)  # 300,000 / 100,000
    assert report.trade_count == 3


def test_payoff_ratio_is_the_average_ratio_not_the_total_ratio():
    """PF와 손익비는 다르다. 같은 거래에서 값이 갈리는 것을 고정한다.

    이 둘을 바꿔 읽어서 진단을 틀린 적이 있다(2026-08-26) — PF를 손익비로
    읽고 "승자가 잘렸다"고 판단했으나 실제로는 승률이 문제였다.
    """
    entered = datetime(2026, 1, 1)
    trades = [
        make_trade("100", entered, entered + timedelta(days=1), 10_000, 11_000, qty=100),
        make_trade("200", entered, entered + timedelta(days=1), 10_000, 9_000, qty=100),
        make_trade("300", entered, entered + timedelta(days=1), 10_000, 12_000, qty=100),
    ]
    report = build_report(make_result(closed_trades=trades), initial_cash=10_000_000)

    # PF = 총이익/총손실 = 300,000 / 100,000 = 3.0
    # 손익비 = 평균이익/평균손실 = 150,000 / 100,000 = 1.5
    assert report.profit_factor == pytest.approx(3.0)
    assert report.payoff_ratio == pytest.approx(1.5)


def test_payoff_ratio_can_exceed_one_while_the_strategy_loses():
    """손익비 > 1을 "괜찮다"로 읽으면 안 된다.

    손익분기 손익비는 (1−승률)/승률이다. 승률 25%면 3.0을 넘겨야 본전인데
    아래는 2.0이라 PF가 1 미만이다 — 실제 전략이 이 모양이었다.
    """
    entered = datetime(2026, 1, 1)
    trades = [
        make_trade("100", entered, entered + timedelta(days=1), 10_000, 12_000, qty=100),
    ] + [
        make_trade(str(i), entered, entered + timedelta(days=1), 10_000, 9_000, qty=100)
        for i in range(200, 203)
    ]
    report = build_report(make_result(closed_trades=trades), initial_cash=10_000_000)

    assert report.win_rate == pytest.approx(0.25)
    assert report.payoff_ratio == pytest.approx(2.0)  # 1을 넘는데
    assert report.profit_factor == pytest.approx(200_000 / 300_000)  # 여전히 진다
    assert report.profit_factor < 1


def test_payoff_ratio_is_none_when_either_side_is_empty():
    entered = datetime(2026, 1, 1)
    trades = [make_trade("100", entered, entered + timedelta(days=1), 10_000, 11_000)]
    report = build_report(make_result(closed_trades=trades), initial_cash=10_000_000)

    assert report.payoff_ratio is None
    assert build_report(make_result(), initial_cash=10_000_000).payoff_ratio is None


def test_profit_factor_is_none_without_any_losing_trade():
    entered = datetime(2026, 1, 1)
    trades = [make_trade("100", entered, entered + timedelta(days=1), 10_000, 11_000)]
    report = build_report(make_result(closed_trades=trades), initial_cash=10_000_000)

    assert report.profit_factor is None


def test_win_rate_and_profit_factor_are_none_without_any_trades():
    report = build_report(make_result(), initial_cash=10_000_000)
    assert report.win_rate is None
    assert report.profit_factor is None
    assert report.trade_count == 0


def test_breakeven_trade_does_not_count_as_a_win():
    entered = datetime(2026, 1, 1)
    trades = [make_trade("100", entered, entered + timedelta(days=1), 10_000, 10_000)]
    report = build_report(make_result(closed_trades=trades), initial_cash=10_000_000)

    assert report.win_rate == 0.0


# --- 평균 보유일 ----------------------------------------------------------------


def test_average_holding_days_across_trades():
    entered = datetime(2026, 1, 1)
    trades = [
        make_trade("100", entered, entered + timedelta(days=2), 10_000, 11_000),
        make_trade("200", entered, entered + timedelta(days=8), 10_000, 11_000),
    ]
    report = build_report(make_result(closed_trades=trades), initial_cash=10_000_000)

    assert report.avg_holding_days == pytest.approx(5.0)


def test_average_holding_days_is_none_without_any_trades():
    report = build_report(make_result(), initial_cash=10_000_000)
    assert report.avg_holding_days is None


# --- 총 거래비용 비중 -------------------------------------------------------------


def test_cost_ratio_divides_by_initial_cash():
    report = build_report(make_result(total_costs=50_000), initial_cash=10_000_000)
    assert report.cost_ratio == pytest.approx(0.005)


# --- 표본 경고 (01문서 §5.1 — 최소 거래 30건) ------------------------------------


def test_sample_warning_below_minimum():
    entered = datetime(2026, 1, 1)
    trades = [
        make_trade(str(i), entered, entered + timedelta(days=1), 10_000, 11_000)
        for i in range(MIN_TRADE_SAMPLE - 1)
    ]
    report = build_report(make_result(closed_trades=trades), initial_cash=10_000_000)
    assert report.sample_warning is True


def test_no_sample_warning_at_minimum():
    entered = datetime(2026, 1, 1)
    trades = [
        make_trade(str(i), entered, entered + timedelta(days=1), 10_000, 11_000)
        for i in range(MIN_TRADE_SAMPLE)
    ]
    report = build_report(make_result(closed_trades=trades), initial_cash=10_000_000)
    assert report.sample_warning is False


# --- 입력 검증 ------------------------------------------------------------------


def test_nonpositive_initial_cash_is_rejected():
    with pytest.raises(ValueError):
        build_report(make_result(), initial_cash=0)


# --- 소르티노 / 칼마 / 최장 무수익 기간 -----------------------------------------


def test_sortino_is_none_without_losing_days():
    """손실일이 없으면 하방편차가 0이라 비율이 정의되지 않는다 — 무한대를 흉내내지 않는다."""
    curve = [(date(2025, 1, i + 1), 10_000_000 + i * 100_000) for i in range(5)]
    report = build_report(make_result(equity_curve=curve), initial_cash=10_000_000)
    assert report.sortino is None


def test_sortino_exceeds_sharpe_when_upside_is_volatile():
    """샤프는 상방 변동에도 벌점을 준다. 큰 상승 하나가 섞이면 둘이 갈린다.

    이 갈림이 소르티노를 넣은 이유다 — 추세 추종처럼 수익 분포가 비대칭인
    전략에서 샤프만 보면 실제 위험과 무관한 벌점을 읽게 된다.
    """
    values = [10_000_000, 10_100_000, 10_050_000, 10_150_000, 13_000_000]
    curve = [(date(2025, 1, i + 1), v) for i, v in enumerate(values)]
    report = build_report(make_result(equity_curve=curve), initial_cash=10_000_000)
    assert report.sortino > report.sharpe


def test_calmar_is_cagr_over_mdd():
    curve = [
        (date(2025, 1, 1), 10_000_000),
        (date(2025, 7, 1), 8_000_000),  # 고점 대비 −20%
        (date(2026, 1, 1), 12_000_000),
    ]
    report = build_report(make_result(equity_curve=curve), initial_cash=10_000_000)
    assert report.mdd == pytest.approx(0.2)
    assert report.calmar == pytest.approx(report.cagr / 0.2)


def test_calmar_is_none_without_a_drawdown():
    """MDD가 0이면 나눌 수 없다."""
    curve = [(date(2025, 1, 1), 10_000_000), (date(2026, 1, 1), 12_000_000)]
    report = build_report(make_result(equity_curve=curve), initial_cash=10_000_000)
    assert report.mdd == 0.0
    assert report.calmar is None


def test_longest_underwater_counts_calendar_days_to_recovery():
    curve = [
        (date(2025, 1, 1), 10_000_000),  # 전고점
        (date(2025, 3, 1), 9_000_000),
        (date(2025, 6, 1), 9_500_000),
        (date(2025, 7, 1), 10_000_000),  # 회복 (1/1 이후 181일)
        (date(2025, 8, 1), 11_000_000),
    ]
    report = build_report(make_result(equity_curve=curve), initial_cash=10_000_000)
    assert report.longest_underwater_days == 181


def test_longest_underwater_includes_an_unrecovered_tail():
    """곡선이 끝날 때까지 회복 못 한 구간도 센다.

    빼면 지표가 낙관 쪽으로 치우친다 — 진행 중인 낙폭이 대개 가장 길다.
    """
    curve = [
        (date(2025, 1, 1), 10_000_000),
        (date(2025, 2, 1), 9_000_000),
        (date(2026, 1, 1), 9_500_000),  # 끝까지 미회복 (365일)
    ]
    report = build_report(make_result(equity_curve=curve), initial_cash=10_000_000)
    assert report.longest_underwater_days == 365


def test_longest_underwater_is_zero_for_a_monotonic_curve():
    curve = [(date(2025, 1, i + 1), 10_000_000 + i * 100_000) for i in range(5)]
    report = build_report(make_result(equity_curve=curve), initial_cash=10_000_000)
    assert report.longest_underwater_days == 0

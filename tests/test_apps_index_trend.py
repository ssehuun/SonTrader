"""지수 이평 추세 필터 테스트 (S1 — 하네스 교정).

**이 파일의 목적은 전략 검증이 아니라 배선 검증이다.** S1은 채택 후보가
아니고, 알려진 효과로 하네스를 맞춰보는 데 쓴다. 그래서 여기서 고정하는 것도
"수익이 나는가"가 아니라 **"루프가 규칙대로 도는가"**다.

가장 중요한 것은 `test_buy_and_hold_makes_no_trades` — 처음에 이게 36건을
내면서 스톱이 안 꺼졌다는 것을 드러냈다.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from sontrader.apps.index_trend import (
    ETF_COST,
    N_GRID,
    equity_rows,
    run,
    sma_signals,
    sweep,
)
from sontrader.apps.report import build_report
from sontrader.core.types import Bar, ExitReason

SYMBOL = "2001"
START = date(2020, 1, 1)


def make_bars(closes: list[float], *, opens: list[float] | None = None) -> list[Bar]:
    """하루 간격 지수 봉. 시가를 따로 주면 "다음 봉 시가 체결"을 눈으로 따라갈 수 있다.

    **기본 시가를 종가보다 1% 낮게 둔다.** 같게 두면 매수가 현금 부족으로
    거부된다 — `entry_weight=1.0`이라 수량이 `equity/종가`로 잡히는데, 체결가에
    슬리피지가 붙고 수수료까지 더해지면 현금을 넘어선다(T21의 메커니즘).
    실제 지수 데이터에서는 거부가 0건이었지만 **합성 가격에서는 걸린다** —
    즉 이건 데이터 운이지 구조적 안전이 아니다.
    """
    opens = opens if opens is not None else [c * 0.99 for c in closes]
    return [
        Bar(
            symbol=SYMBOL,
            ts=datetime.combine(START + timedelta(days=i), time.min),
            open=o,
            high=max(o, c),
            low=min(o, c),
            close=c,
            volume=1,
        )
        for i, (o, c) in enumerate(zip(opens, closes, strict=True))
    ]


# --- SMA 신호 ----------------------------------------------------------------


def test_sma_window_is_not_filled_until_n_bars():
    """앞 `n-1`개는 창이 안 차 신호가 없다. 이 구간을 True로 두면 워밍업 없이
    거래가 시작돼 모든 `N`의 창이 달라진다."""
    signals = sma_signals(make_bars([1, 2, 3, 4, 5]), 3)

    assert signals[:2] == [False, False]
    assert signals[2:] == [True, True, True]  # 상승이라 종가 > SMA


def test_signal_is_close_above_its_own_moving_average():
    # 종가 [10,10,10,7] · SMA(3) 마지막 = (10+10+7)/3 = 9 → 7 > 9 은 거짓
    signals = sma_signals(make_bars([10, 10, 10, 7]), 3)

    assert signals[2] is False  # 10 > SMA(3)=10 이 아니다 (강부등호)
    assert signals[3] is False


def test_equality_is_not_a_signal():
    """`종가 > SMA`는 강부등호다. 평평한 구간에서 종가 == SMA인데 진입하면
    무의미한 왕복이 늘어난다."""
    signals = sma_signals(make_bars([10, 10, 10]), 3)

    assert signals[2] is False  # 10 > 10 이 아니다


def test_sma_rejects_nonsense_period():
    with pytest.raises(ValueError, match="n must be"):
        sma_signals(make_bars([1, 2]), 0)


# --- 배선: 스톱이 정말 꺼졌는가 ------------------------------------------------


def test_buy_and_hold_makes_no_trades():
    """🔴 이 테스트가 실제로 버그를 잡았다.

    처음에 `stop_loss_pct=-0.999`만 주고 스톱을 껐다고 생각했는데 S0이
    **STOP으로 36번 청산**됐다. 3구간 스톱의 나머지 둘이 살아 있었기 때문이다 —
    가격이 `breakeven_trigger`(+5%)를 넘으면 스톱이 진입가로 올라가고, 그
    뒤로는 ATR 트레일링이 붙는다. 고정 손절은 첫 구간일 뿐이다.
    """
    bars = make_bars([100 + i for i in range(60)])  # 계속 오른다 → 본전 이동 구간

    result = run(bars, n=None, warmup=10, initial_cash=10_000_000).result

    assert result.closed_trades == (), "매수보유가 청산됐다 — 스톱이 안 꺼졌다"
    assert len(result.final_positions) == 1


def test_a_dropped_signal_closes_the_position_with_reason_signal():
    """S1 규칙의 청산 절반. 스톱으로는 표현할 수 없다 — 손실이 나서가 아니라
    살 이유가 없어져서 판다."""
    # 20봉 상승 후 급락 → SMA(5) 아래로 내려간다
    closes = [100 + i for i in range(20)] + [80, 78, 76, 74, 72]
    bars = make_bars(closes)

    result = run(bars, n=5, warmup=6, initial_cash=10_000_000).result

    assert result.closed_trades
    assert result.closed_trades[-1].exit_reason is ExitReason.SIGNAL
    assert result.final_positions == ()


# --- 배선: look-ahead 차단 ----------------------------------------------------


def test_entry_fills_at_the_next_bar_open_not_the_signal_close():
    """신호는 종가로 판정하고 체결은 **다음 봉 시가**다. 같은 봉 종가에
    체결하면 그 종가를 보고 판단한 뒤 그 가격에 사는 셈이라 look-ahead다."""
    # 신호 봉의 종가(20)와 다음 봉 시가(15)를 벌려 어느 쪽에 체결됐는지 본다.
    #
    # **시가를 종가보다 위로 둘 수 없다.** 수량이 `현금/신호봉 종가`로 잡히는데
    # 체결가가 그보다 높으면 현금을 넘어 거부된다(`entry_weight=1.0`). 상방 갭에
    # 진입이 통째로 사라지는 이 성질 자체가 T21이다.
    closes = [10, 10, 10, 20, 20, 20]
    opens = [9, 9, 9, 9, 15, 15]
    bars = make_bars(closes, opens=opens)

    result = run(bars, n=3, warmup=3, initial_cash=10_000_000).result

    assert result.fills, "진입이 아예 없다"
    # index 3에서 신호(20 > SMA3=13.3) → index 4 **시가 15**에 체결.
    # 신호 봉 종가 20에 체결됐다면 look-ahead다.
    assert 15 <= result.fills[0].price < 20


# --- 스윕 --------------------------------------------------------------------


def test_sweep_aligns_every_n_to_the_same_start():
    """🔴 함정 1. `N`마다 자기 워밍업 뒤부터 시작하면 창이 달라져 단조성
    비교가 무의미해진다."""
    bars = make_bars([100 + (i % 7) for i in range(400)])

    s0, runs = sweep(bars, grid=(5, 20, 50), initial_cash=10_000_000)

    days = {run_.result.equity_curve[0][0] for run_ in runs}
    assert len(days) == 1, "N마다 시작일이 다르다 — 창이 어긋났다"
    assert s0.result.equity_curve[0][0] == days.pop()


def test_sweep_returns_one_run_per_grid_point():
    bars = make_bars([100 + (i % 5) for i in range(300)])

    _, runs = sweep(bars, grid=(5, 10, 20), initial_cash=10_000_000)

    assert [r.n for r in runs] == [5, 10, 20]


def test_run_refuses_a_window_shorter_than_the_warmup():
    """조용히 빈 결과를 돌려주면 "효과가 없다"와 구별되지 않는다."""
    with pytest.raises(ValueError, match="창이 너무 짧다"):
        run(make_bars([1, 2, 3]), n=2, warmup=10, initial_cash=1_000_000)


# --- 비용 --------------------------------------------------------------------


def test_etf_cost_is_a_fifth_of_the_single_stock_cost():
    """ETF는 **증권거래세가 면제**다. 개별주 0.708%를 그대로 쓰면 S1을
    부당하게 불리하게 판정한다."""
    round_trip = 2 * (ETF_COST.commission_rate + ETF_COST.slippage_bps / 10_000)

    assert ETF_COST.tax_rate == 0.0
    assert round_trip == pytest.approx(0.0015, abs=1e-5)  # 왕복 0.15%


def test_the_cost_config_actually_reaches_the_broker():
    """반증 ④b는 비용을 올려도 결론이 사는지를 본다 — 그러려면 올린 비용이
    실제로 체결에 닿아야 한다.

    **수수료로 잰다. 슬리피지로는 못 잰다** — 슬리피지는 체결가를 올려
    `entry_weight=1.0`에서 매수를 거부시키고, 거부가 늘면 회전이 줄어
    **비용을 올렸는데 성과가 좋아진다.** 실제로 그렇게 나왔다(10배 슬리피지에서
    최종자산이 4.7배). 그건 배선 오류가 아니라 T21의 부작용이라, 비용 전달을
    확인하는 데는 수수료가 맞는 손잡이다.
    """
    from dataclasses import replace

    bars = make_bars([100 + (i % 11) for i in range(300)])
    cheap = run(bars, n=20, warmup=50, initial_cash=10_000_000)
    dear = run(
        bars,
        n=20,
        warmup=50,
        initial_cash=10_000_000,
        broker_config=replace(ETF_COST, commission_rate=ETF_COST.commission_rate * 10),
    )

    assert len(dear.result.closed_trades) == len(cheap.result.closed_trades)
    assert dear.result.total_costs > cheap.result.total_costs


# --- 산출물 ------------------------------------------------------------------


def test_grid_includes_the_low_end_so_the_peak_has_a_left_arm():
    """반증 ①이 "완만한 봉우리 하나"인데, 최적이 격자 끝에 서면 내부 최적인지
    경계인지 구별할 수 없다."""
    assert min(N_GRID) == 5
    assert len(N_GRID) == 14


def test_equity_rows_pair_the_two_series_on_the_same_days():
    bars = make_bars([100 + (i % 9) for i in range(300)])
    s0, runs = sweep(bars, grid=(20,), initial_cash=10_000_000)

    rows = equity_rows(s0, runs[0])

    assert rows
    assert [r[0] for r in rows] == [day for day, _ in s0.result.equity_curve]
    assert all(flag in (0, 1) for *_, flag in rows)


def test_report_metrics_are_computable_from_the_run():
    """S1 판정에 필요한 여섯 지표가 전부 나와야 한다."""
    bars = make_bars([100 + (i % 13) for i in range(400)])
    _, runs = sweep(bars, grid=(50,), initial_cash=10_000_000)

    report = build_report(runs[0].result, initial_cash=10_000_000)

    assert report.mdd is not None
    assert report.longest_underwater_days is not None
    for name in ("cagr", "sharpe", "sortino", "calmar"):
        assert hasattr(report, name)

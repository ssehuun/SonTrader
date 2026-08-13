"""백테스트 성과 지표 (01문서 §5.3). apps/report.py.

`BacktestResult`(자산 곡선 + 청산된 거래 목록)만으로 계산되는 순수 함수들이다
— DB도 브로커도 필요 없다. 계산 대상: CAGR / 샤프 / MDD / 승률 / 손익비 /
평균 보유일 / 거래 횟수 / 총 거래비용 비중.

## 명시적으로 확정한 것들

**1. 거래 단위는 포지션(진입~청산) 하나다, 체결(Fill) 하나가 아니다.**
매수 1건 + 매도 1건이 한 거래다. `apps/backtest.py`가 이미 이 짝짓기를
`ClosedTrade`로 해뒀으므로(부분체결·재진입이 섞여도 안전하게), 여기서
다시 풀지 않는다.

**2. 승률·손익비는 수수료·세금을 빼지 않은 체결가 기준 손익으로 계산한다.**
`Fill.price`는 슬리피지는 반영하지만 수수료·증권거래세는 별도로 현금에서
차감돼 개별 체결에 남지 않는다(설계상 `data/db.py`의 `fills` 테이블에 그
컬럼이 없다). 그래서 개별 거래의 승/패 판정은 가격 기준으로 하고, 비용의
영향은 "총 거래비용 비중" 하나로 따로 본다 — 두 지표를 억지로 합치면
어느 쪽도 정확하지 않게 된다.

**3. CAGR은 달력일, 샤프는 사이클(≈거래일) 단위로 연환산한다.**
CAGR은 실제 경과 시간에 대한 연복리 수익률이라는 정의상 달력일(365)이
맞다. 반면 `equity_curve`의 한 점은 `watchlist_snapshots`가 있는 날 —
즉 실제 거래일 하나다(주말·휴장일엔 스냅샷 자체가 없다). 그래서 일별
수익률의 표준편차를 연율화할 때는 관례적인 거래일수(252)를 곱한다.

**4. 총 거래비용 비중의 분모는 초기 자본이다.** 01문서 §7의 우려("자본
1,000만원 기준 거래비용 비중이 크다")가 시작 자본 대비 비용을 말하고
있으므로 그 기준을 그대로 따른다.

**5. 계산할 수 없는 지표는 0이 아니라 `None`이다.** 0으로 채우면 "손실
없음"과 "표본 없음"이 구분되지 않는다 — 01문서 §5.1 "최소 거래 표본
30건 미만이면 결과를 신뢰하지 않는다"는 원칙과 같은 이유로, 표본 부족을
조용히 감추지 않는다. `sample_warning`이 이 원칙을 명시적으로 드러낸다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import fmean, pstdev

from sontrader.apps.backtest import BacktestResult, ClosedTrade

MIN_TRADE_SAMPLE = 30  # 01문서 §5.1
_TRADING_DAYS_PER_YEAR = 252
_CALENDAR_DAYS_PER_YEAR = 365.0


@dataclass(frozen=True)
class PerformanceReport:
    cagr: float | None
    sharpe: float | None
    mdd: float
    win_rate: float | None
    profit_factor: float | None
    avg_holding_days: float | None
    trade_count: int
    cost_ratio: float
    sample_warning: bool  # trade_count < MIN_TRADE_SAMPLE (01문서 §5.1)


def build_report(result: BacktestResult, *, initial_cash: int) -> PerformanceReport:
    if initial_cash <= 0:
        raise ValueError(f"initial_cash must be positive: {initial_cash}")

    equity_values = [equity for _, equity in result.equity_curve]
    trades = result.closed_trades

    return PerformanceReport(
        cagr=_cagr(result.equity_curve, initial_cash),
        sharpe=_sharpe(equity_values),
        mdd=_mdd(equity_values, initial_cash),
        win_rate=_win_rate(trades),
        profit_factor=_profit_factor(trades),
        avg_holding_days=_avg_holding_days(trades),
        trade_count=len(trades),
        cost_ratio=result.total_costs / initial_cash,
        sample_warning=len(trades) < MIN_TRADE_SAMPLE,
    )


def _cagr(equity_curve, initial_cash: int) -> float | None:
    if not equity_curve:
        return None
    start_date, _ = equity_curve[0]
    end_date, final_equity = equity_curve[-1]
    days = (end_date - start_date).days
    if days <= 0 or final_equity <= 0:
        return None
    years = days / _CALENDAR_DAYS_PER_YEAR
    return (final_equity / initial_cash) ** (1.0 / years) - 1.0


def _daily_returns(equity_values: list[int]) -> list[float]:
    returns = []
    for prev, curr in zip(equity_values, equity_values[1:], strict=False):
        if prev <= 0:
            continue
        returns.append(curr / prev - 1.0)
    return returns


def _sharpe(equity_values: list[int]) -> float | None:
    returns = _daily_returns(equity_values)
    if len(returns) < 2:
        return None
    std = pstdev(returns)
    if std == 0:
        return None
    # 무위험수익률 0 가정. 위 docstring 3번 — 사이클이 거래일 단위이므로 252로 연환산.
    return fmean(returns) / std * math.sqrt(_TRADING_DAYS_PER_YEAR)


def _mdd(equity_values: list[int], initial_cash: int) -> float:
    peak = initial_cash
    worst = 0.0
    for equity in equity_values:
        peak = max(peak, equity)
        if peak <= 0:
            continue
        worst = max(worst, (peak - equity) / peak)
    return worst


def _win_rate(trades: Sequence[ClosedTrade]) -> float | None:
    if not trades:
        return None
    wins = sum(1 for t in trades if _pnl(t) > 0)
    return wins / len(trades)


def _profit_factor(trades: Sequence[ClosedTrade]) -> float | None:
    if not trades:
        return None
    gains = sum(_pnl(t) for t in trades if _pnl(t) > 0)
    losses = -sum(_pnl(t) for t in trades if _pnl(t) < 0)
    if losses == 0:
        return None  # 손실 거래가 없으면 비율이 정의되지 않는다 — 무한대를 흉내내지 않는다
    return gains / losses


def _avg_holding_days(trades: Sequence[ClosedTrade]) -> float | None:
    if not trades:
        return None
    return fmean((t.exit_at.date() - t.entered_at.date()).days for t in trades)


def _pnl(trade: ClosedTrade) -> float:
    return (trade.exit_price - trade.entry_price) * trade.qty

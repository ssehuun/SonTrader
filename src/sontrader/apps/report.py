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
    sortino: float | None
    calmar: float | None
    longest_underwater_days: int | None
    win_rate: float | None
    profit_factor: float | None
    payoff_ratio: float | None
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
        sortino=_sortino(equity_values),
        calmar=_calmar(_cagr(result.equity_curve, initial_cash), _mdd(equity_values, initial_cash)),
        longest_underwater_days=_longest_underwater_days(result.equity_curve, initial_cash),
        mdd=_mdd(equity_values, initial_cash),
        win_rate=_win_rate(trades),
        profit_factor=_profit_factor(trades),
        payoff_ratio=_payoff_ratio(trades),
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


def _sortino(equity_values: list[int]) -> float | None:
    """소르티노 = 수익 / **하방** 표준편차. 샤프의 교정이다.

    샤프는 상방 변동성에도 벌점을 준다 — 크게 오른 날이 지표를 깎는다.
    추세 추종처럼 **수익 분포가 비대칭인** 전략에서 그 벌점이 실제 위험과
    무관하게 커진다. 소르티노는 음수 수익만 분모에 넣는다.

    둘을 함께 본다. **소르티노가 샤프보다 훨씬 크면 상방 변동이 컸다는 뜻**이고,
    그건 벌점이 아니라 전략의 성질이다.
    """
    returns = _daily_returns(equity_values)
    if len(returns) < 2:
        return None
    downside = [r for r in returns if r < 0]
    if not downside:
        return None  # 손실일이 없으면 비율이 정의되지 않는다
    # 하방편차는 음수 수익만 제곱평균한다 — 표본 표준편차가 아니다
    dd = math.sqrt(sum(r * r for r in downside) / len(returns))
    if dd == 0:
        return None
    return fmean(returns) / dd * math.sqrt(_TRADING_DAYS_PER_YEAR)


def _calmar(cagr: float | None, mdd: float) -> float | None:
    """칼마 = CAGR / MDD. **"낙폭 1%p당 몇 %의 수익을 벌었나."**

    S1(지수 추세 필터)의 주장이 "MDD는 줄고 CAGR은 유지"인데, 그 맞바꿈을
    **한 숫자로** 표현한 것이 칼마다. 두 지표를 따로 보면 "CAGR이 조금 줄고
    MDD도 조금 줄었다"에서 어느 쪽이 이겼는지 판단이 갈린다.

    샤프·소르티노와 달리 **일별 변동이 아니라 최악의 한 사건**을 분모에 쓴다.
    변동성은 낮지만 한 번 크게 무너지는 전략을 샤프는 잘 잡지 못한다.
    """
    if cagr is None or mdd <= 0:
        return None
    return cagr / mdd


def _longest_underwater_days(equity_curve, initial_cash: int) -> int | None:
    """전고점을 회복하기까지 걸린 **최장 달력일**.

    MDD는 낙폭의 **깊이**만 말한다. 30% 빠졌다가 3개월에 회복하는 것과 3년이
    걸리는 것은 완전히 다른 전략인데 MDD로는 구분되지 않는다.

    **곡선이 끝날 때까지 회복 못 한 구간도 센다** — 진행 중인 낙폭이 대개
    가장 길고, 빼면 지표가 낙관 쪽으로 치우친다.
    """
    if not equity_curve:
        return None
    peak = initial_cash
    peak_date = equity_curve[0][0]
    longest = 0
    underwater = False
    for date, equity in equity_curve:
        if equity >= peak:
            # 회복한 날까지 센다 — 고점에서 회복까지가 "물속에 있던 기간"이다.
            # 회복 직전 관측치에서 끊으면 마지막 구간이 빠진다.
            if underwater:
                longest = max(longest, (date - peak_date).days)
            peak = equity
            peak_date = date
            underwater = False
        else:
            underwater = True
            longest = max(longest, (date - peak_date).days)
    return longest


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


def _payoff_ratio(trades: Sequence[ClosedTrade]) -> float | None:
    """손익비 = 평균이익 / 평균손실. **Profit Factor와 다르다.**

    `PF = 손익비 × 승률/(1−승률)` — PF는 승률을 이미 품고 있고 손익비는
    승률과 독립이다. 그래서 둘을 함께 봐야 "왜 지는가"가 갈린다:

    - 손익비는 높은데 PF가 낮다 → **덜 맞힌다.** 진입 신호 문제
    - 손익비가 낮은데 승률이 높다 → **승자를 일찍 자른다.** 청산 문제

    둘을 바꿔 읽어서 실제로 진단을 틀린 적이 있다(2026-08-26). PF 0.40을
    손익비로 읽고 "승자가 잘렸다"고 판단했는데, 실제 손익비는 2.28로
    승자가 패자보다 2배 넘게 컸다. 문제는 승률이었다.

    손익분기 손익비는 `(1−승률)/승률`이다 — 승률 22.6%면 3.42를 넘겨야
    본전이다. 이 값만 보고 "1을 넘으니 괜찮다"고 읽으면 안 된다.

    PF와 같은 **원 단위**로 잰다. 수익률(%) 기준으로 재면 포지션 크기가
    빠져 두 지표를 나란히 놓을 수 없다.
    """
    gains = [_pnl(t) for t in trades if _pnl(t) > 0]
    losses = [-_pnl(t) for t in trades if _pnl(t) < 0]
    if not gains or not losses:
        return None  # 한쪽이 비면 비율이 정의되지 않는다 — 무한대를 흉내내지 않는다
    return (sum(gains) / len(gains)) / (sum(losses) / len(losses))


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

"""지수 이동평균 추세 필터 (S1) — **백테스트 교정용**.

## 이건 전략 채택 후보가 아니다

우리는 백테스트를 **알려진 정답으로 맞춰본 적이 한 번도 없다.** 지금까지 잰
것은 전부 "이 전략이 지는가"였고 전부 졌는데, **백테스트가 고장 나 있어도
똑같이 진다.** 기준선 v1·v2가 부기 버그로 통째로 무효였던 전례가 정확히
그것이다.

지수 추세추종은 수십 개 시장에서 문서화된 효과다. **재현이 안 되면 효과가
없는 게 아니라 배관이 새는 것**이라고 읽을 수 있다는 점이 이 전략을 고른
이유다. 통과해도 실전에 올리지 않는다 — 나오는 것은 KODEX 200을 사고파는
봇이지 종목을 고르는 봇이 아니다.

## 그래서 별도 계산이면 무의미하다

이평을 구해 수익률을 내는 것은 짧은 스크립트로 되지만 **그건 백테스트를 안
건드린다.** 이 모듈은 반드시 `apps/backtest.py`의 `replay()`를 통과한다 —
같은 재생 루프, 같은 `core/` 게이트·청산, 같은 `SimBroker` 체결.

여기서 하는 일은 **입력을 만드는 것뿐**이다:

| 무엇 | 어떻게 |
|---|---|
| 봉 | `index_candles_1d`의 지수를 `Bar`로 (`data/index_prices.py`가 수집) |
| 진입 신호 | `종가 > SMA(N)`인 날의 워치리스트에 종목을 넣는다 |
| 청산 신호 | 워치리스트에서 뺀다 — `StrategyConfig.exit_when_dropped`가 받는다 |

## 🔴 함정 셋

**1. SMA 워밍업을 모든 `N`에 대해 맞춘다.** `N=250`이면 첫 249봉은 신호가
없다. 각 `N`이 자기 워밍업 뒤부터 시작하면 **창이 달라져 단조성 비교가
무의미해진다.** 그래서 격자에서 가장 긴 `N`에 맞춰 **전체 시작일을 통일**한다.

**2. 미래 참조.** `종가 > SMA(N)`의 SMA는 **그 종가를 포함**한다. 판정은 종가
확정 후이고 체결은 **다음 봉 시가**다. 이 성질은 우연이 아니라 배선으로
보장된다 — `Context.now`가 그날 봉의 `ts`와 같아서 `InMemoryBarView`는 그날
종가까지 보여주고(`bisect_right`), `SimBroker._next_bar()`는 `order.ts`
**이후** 봉을 찾으므로 그날 봉을 건너뛴다.

**3. 비용은 ETF 기준이다.** 규약의 0.708%는 개별주 값이고 **거래세 0.2%가
ETF에는 없다.** S1.3이 재산출한 **왕복 0.15%**를 쓴다 — 수수료 0.028% +
스프레드 0.10% + 충격 0.02%. 가장 불리한 초기 저가 구간 기준이라 최근
구간에서는 실제로 더 싸다.

## 한계 — 판정문에 그대로 옮긴다

**백테스트의 일부만 검정한다.** ETF 일봉이 DB에 없어(`069500`·`102110` 둘 다
0행) **지수로 재생**한다.

| 검정된다 | 안 된다 |
|---|---|
| 재생 루프 · 청산 · 리포트 지표 | 유니버스 · 필터 · 주문 경로 |

**"백테스트 전체를 검정했다"고 쓰지 않는다. 부분 교정이다.**
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.adapters.broker_sim import SimBrokerConfig
from sontrader.apps.backtest import BacktestResult, replay
from sontrader.core.gate import GateConfig
from sontrader.core.strategy import EntryTrigger, StrategyConfig
from sontrader.core.types import Bar, ExitRule
from sontrader.data import db
from sontrader.engine.loop import CycleConfig

log = logging.getLogger(__name__)

# 탐색 격자 (S1.2). **하한을 60이 아니라 5로 둔다** — 최적이 격자 끝에 서면
# 내부 최적인지 경계인지 구별할 수 없고, 왼쪽 팔이 없으면 "봉우리"라 부를 수
# 없다. 저N 구간이 회전비용으로 매끄럽게 나빠지는 것이 정상이고, 그 예측이
# 어긋나면(N=15만 유독 좋다면) 잡음 신호다.
N_GRID: tuple[int, ...] = (5, 10, 15, 20, 30, 40, 50, 60, 80, 100, 120, 150, 200, 250)

# ETF 왕복 0.15% (S1.3). 개별주 0.708%의 약 1/5 — **거래세 0.2%가 없다**.
#
#   왕복 = 2 × (수수료 0.014% + 슬리피지 0.061%) = 0.028% + 0.122% = 0.150%
#
# 슬리피지 6.1bp에 스프레드(0.10% 왕복)와 시장충격(0.02% 왕복)을 함께 담았다.
# 스프레드는 ETF 호가단위가 5원 고정이라 가격이 오를수록 싸진다 — 가장 불리한
# 초기 저가 구간(10,000원 → 왕복 0.10%)을 기준으로 잡은 보수값이다.
ETF_COST = SimBrokerConfig(
    commission_rate=0.014 / 100,
    tax_rate=0.0,  # ETF는 증권거래세 면제
    slippage_bps=6.1,
    settlement_days=2,
)


@dataclass(frozen=True)
class TrendRun:
    """한 번의 실행 결과. `N=None`이면 S0(매수보유)."""

    n: int | None
    result: BacktestResult
    round_trips_per_year: float
    in_market_ratio: float  # 사이클 중 포지션을 들고 있던 비율
    years: float

    @property
    def label(self) -> str:
        return "S0" if self.n is None else f"S1(N={self.n})"


def load_index_bars(engine: Engine, code: str, start: date, end: date) -> list[Bar]:
    """지수 일봉 → `Bar`. 종목 백테스트와 **같은 타입**이라 같은 루프를 탄다.

    `Bar`의 가격 필드는 `int` 힌트지만 지수는 소수다(6912.37). 런타임 검사가
    없어 그대로 흐르고, 체결가 반올림은 `SimBroker`가 한다 — 즉 지수를
    "1주 6,912.37원짜리 종목"처럼 다룬다. ETF 일봉이 없어 택한 대리이고,
    그래서 유니버스·필터 경로는 검정되지 않는다(모듈 상단 한계 참고).
    """
    columns = db.index_candles_1d.c
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(columns.date, columns.open, columns.high, columns.low, columns.close)
            .where(columns.code == code, columns.date >= start, columns.date <= end)
            .order_by(columns.date)
        ).all()
    bars: list[Bar] = []
    for row in rows:
        if row.close is None:
            continue  # 종가가 없으면 신호도 체결도 만들 수 없다
        close = row.close
        bars.append(
            Bar(
                symbol=code,
                ts=datetime.combine(row.date, time.min),
                open=row.open if row.open is not None else close,
                high=row.high if row.high is not None else close,
                low=row.low if row.low is not None else close,
                close=close,
                # 지수에 거래량 개념을 넣지 않는다. **0이면 `SimBroker._next_bar()`가
                # 거래정지 봉으로 보고 건너뛴다** — 체결이 통째로 사라진다.
                volume=1,
            )
        )
    return bars


def sma_signals(bars: Sequence[Bar], n: int) -> list[bool]:
    """`종가 > SMA(N)`. 앞 `n-1`개는 창이 안 차 `False`.

    SMA는 **그날 종가를 포함**한다(규칙 그대로). 그래도 look-ahead가 아닌
    이유는 체결이 다음 봉 시가이기 때문이다 — 모듈 상단 함정 2.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1: {n}")
    closes = [float(bar.close) for bar in bars]
    out = [False] * len(closes)
    window = 0.0
    for i, close in enumerate(closes):
        window += close
        if i >= n:
            window -= closes[i - n]
        if i >= n - 1:
            out[i] = close > window / n
    return out


def run(
    bars: Sequence[Bar],
    *,
    n: int | None,
    warmup: int,
    initial_cash: int,
    broker_config: SimBrokerConfig | None = None,
) -> TrendRun:
    """한 조건을 재생한다. **`replay()`를 통과한다 — 별도 계산이 아니다.**

    `warmup`은 **격자 전체가 공유하는** 시작 오프셋이다. 각 `N`이 자기
    워밍업만큼만 건너뛰면 창이 달라져 단조성 비교가 무의미해진다(함정 1).

    `n=None`이면 S0 — 워치리스트에 항상 종목이 있어 한 번 사고 끝까지 든다.
    """
    if warmup >= len(bars):
        raise ValueError(f"warmup({warmup}) >= 봉 개수({len(bars)}) — 창이 너무 짧다")

    symbol = bars[0].symbol
    signals = [True] * len(bars) if n is None else sma_signals(bars, n)
    active = bars[warmup:]

    watchlists: dict[date, list[str]] = {}
    for bar, hold in zip(active, signals[warmup:], strict=True):
        watchlists[bar.ts.date()] = [symbol] if hold else []

    # 스톱·최대보유일이 끼어들면 순수한 추세 필터가 아니게 된다 — S1의 규칙은
    # 이평 하나뿐이고, 다른 청산이 섞이면 무엇이 성과를 만들었는지 못 가른다.
    # 0으로 둘 수 없어(`__post_init__`이 거부한다) **도달 불가능한 값**으로 끈다.
    #
    # 🔴 **`stop_loss_pct`만 낮추면 안 꺼진다.** 처음에 −99.9%만 주고 껐다고
    # 생각했는데 S0(매수보유)이 **STOP으로 36번 청산됐다.** 3구간 스톱의 나머지
    # 둘이 살아 있었기 때문이다 — 가격이 `breakeven_trigger`(기본 +5%)를 넘으면
    # 스톱이 **진입가로 올라가고**(본전 이동) 그 뒤로는 ATR 트레일링이 붙는다.
    # 고정 손절은 세 구간 중 첫 구간일 뿐이다.
    #
    # 그래서 `breakeven_trigger`를 도달 불가능하게 키운다. 그러면 `stop_level()`이
    # 항상 첫 분기에 머물러 고정 손절(−99.9%)만 남고, 그건 발동하지 않는다.
    inert_exit = ExitRule(max_hold_days=10**6, stop_loss_pct=-0.999, breakeven_trigger=1e9)
    config = CycleConfig(
        strategy=StrategyConfig(
            entry_trigger=EntryTrigger.WATCHLIST_RANK,
            entry_weight=1.0,  # 100% 진입 — 지수 하나만 든다
            exit_rule=inert_exit,
            # 신호가 사라지면 판다. 이게 S1 규칙의 청산 절반이고, 스톱으로는
            # 표현할 수 없다(손실이 나서가 아니라 살 이유가 없어져서 판다).
            exit_when_dropped=True,
        ),
        gate=GateConfig(max_positions=1, max_weight=1.0, cooldown_days=0),
        check_killswitch=False,
    )

    result = replay(
        watchlists=watchlists,
        bars={symbol: list(bars)},
        events={},
        initial_cash=initial_cash,
        broker_config=broker_config or ETF_COST,
        cycle_config=config,
        watchlist_ranks={day: {symbol: 1} for day in watchlists},
    )

    days = [bar.ts.date() for bar in active]
    years = max((days[-1] - days[0]).days / 365.25, 1e-9)
    in_market = sum(1 for hold in signals[warmup:] if hold) / len(days)
    return TrendRun(
        n=n,
        result=result,
        round_trips_per_year=len(result.closed_trades) / years,
        in_market_ratio=in_market,
        years=years,
    )


def sweep(
    bars: Sequence[Bar],
    *,
    grid: Sequence[int] = N_GRID,
    initial_cash: int,
    broker_config: SimBrokerConfig | None = None,
) -> tuple[TrendRun, list[TrendRun]]:
    """S0 + 격자 전체를 **순차** 실행한다. (S0, [S1…])

    병렬로 돌리지 않는다 — 메모리가 제약이라는 것이 실측이다(T26). 지수는
    봉이 적어 여유가 있지만, 같은 함수가 종목으로 확장될 때 관성이 남는다.

    **워밍업을 격자 최대값에 맞춰 통일한다** — 함정 1. 이게 없으면 `N`마다
    창이 달라져 단조성 비교가 무의미해진다.
    """
    warmup = max(grid) - 1
    s0 = run(bars, n=None, warmup=warmup, initial_cash=initial_cash, broker_config=broker_config)
    runs = [
        run(bars, n=n, warmup=warmup, initial_cash=initial_cash, broker_config=broker_config)
        for n in grid
    ]
    return s0, runs


def equity_rows(s0: TrendRun, s1: TrendRun) -> list[tuple[date, int, int, int]]:
    """`date,s0_equity,s1_equity,in_market` — 그래프용 CSV 행.

    두 계열의 날짜 집합이 같다(같은 `warmup`을 쓴다). 다르면 그건 배선 오류라
    조용히 맞추지 않고 비운다.
    """
    s1_equity = dict(s1.result.equity_curve)
    # 포지션을 들고 있던 날 = 진입일~청산일 구간. 거래 목록에서 채운다.
    held: set[date] = set()
    for trade in s1.result.closed_trades:
        day = trade.entered_at.date()
        while day <= trade.exit_at.date():
            held.add(day)
            day = date.fromordinal(day.toordinal() + 1)

    rows: list[tuple[date, int, int, int]] = []
    for day, s0_equity in s0.result.equity_curve:
        if day not in s1_equity:
            continue
        rows.append((day, s0_equity, s1_equity[day], 1 if day in held else 0))
    return rows

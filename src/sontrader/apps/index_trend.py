"""지수 이동평균 추세 필터 (S1).

## 재는 것은 둘뿐이다

| | 무엇 | 함수 |
|---|---|---|
| **1단계** | 진입·청산 이평이 **같을 때**(`N`) 수익률 | `sweep()` |
| **2단계** | 진입·청산 이평을 **다르게** 했을 때(`N_in`·`N_out`) 수익률 | `sweep_exit()` |

**둘 다 S0(매수보유)과 나란히 놓는다.** 그 밖의 검사는 두지 않는다.

## 별도 계산이면 무의미하다

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

**2. 신호와 체결이 같은 종가를 쓴다 — 근사이고, 방향은 유리한 쪽이다.**
15:20 종가 동시호가에 집행한다고 두었다(`SimBrokerConfig.fill_at_close`).
실전에서는 15:20에 주문할 때 오늘 종가가 아직 확정되지 않으므로 **15:20
가격을 종가로 대용**하는 것이고, 일봉에는 15:20 가격이 없다. **여기서 나온
수치는 그만큼 실전보다 낙관이다 — 판정문에 그대로 옮긴다.**

시가 체결을 쓰지 않는 이유는 그쪽이 더 큰 왜곡을 끌고 왔기 때문이다. 체결가를
주문 시점에 모르면 수량을 증거금 상한(현재가 × 1.30)으로 잡아야 하고, 거기서
분할 체결·미투입 현금·진입 첫날 노출 부족이 딸려 나온다. **그 배관 편차가
재려던 효과보다 컸다** (M007·M008).

**3. 비용은 ETF 기준이다.** 규약의 0.708%는 개별주 값이고 **거래세 0.2%가
ETF에는 없다.** 왕복 **0.15%**를 쓴다 — 수수료 0.028% + 스프레드 0.10% +
충격 0.02%. 가장 불리한 초기 저가 구간 기준이라 최근 구간에서는 더 싸다.

## 한계 — 판정문에 그대로 옮긴다

**백테스트의 일부만 탄다.** ETF 일봉이 DB에 없어(`069500`·`102110` 둘 다
0행) **지수로 재생**한다.

| 탄다 | 안 탄다 |
|---|---|
| 재생 루프 · 청산 · 리포트 지표 | 유니버스 · 필터 |
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
from sontrader.core.diff import DiffConfig
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
    fill_at_close=True,  # 15:20 종가 동시호가 — 모듈 상단 함정 2
)

# 종가 동시호가 수량 환산의 여유분. 체결가를 이미 알므로 증거금 상한(1.30)이
# 필요 없고, **슬리피지 6.1bp + 수수료 1.4bp + 반올림**만 덮으면 된다. 0.2%는
# 그 합(0.075%)의 2.7배다.
#
# 손잡이가 아니다 — 전략이 무엇을 살지 정하는 값이 아니라 집행 계층 상수다.
# 남는 현금 0.2%가 노는 대가는 CAGR 약 −0.006%p다.
CLOSE_FILL_BUFFER = 0.002


@dataclass(frozen=True)
class TrendRun:
    """한 번의 실행 결과. `N=None`이면 S0(매수보유)."""

    n: int | None
    result: BacktestResult
    round_trips_per_year: float
    in_market_ratio: float  # 사이클 중 포지션을 들고 있던 비율
    years: float
    n_out: int | None = None  # 청산 이평. `None`이면 진입과 같다(대칭 — 1단계)

    @property
    def label(self) -> str:
        if self.n is None:
            return "S0"
        if self.n_out is None or self.n_out == self.n:
            return f"S1(N={self.n})"
        return f"S1(N_in={self.n},N_out={self.n_out})"


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


def sma(bars: Sequence[Bar], n: int) -> list[float | None]:
    """단순이동평균. 창이 안 찬 앞 `n-1`개는 `None`이다.

    `None`으로 두는 것이 0.0으로 두는 것보다 안전하다 — 0과 비교하면 모든
    종가가 이평 위가 되어 워밍업 구간이 통째로 진입 신호가 된다.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1: {n}")
    closes = [float(bar.close) for bar in bars]
    out: list[float | None] = [None] * len(closes)
    window = 0.0
    for i, close in enumerate(closes):
        window += close
        if i >= n:
            window -= closes[i - n]
        if i >= n - 1:
            out[i] = window / n
    return out


def sma_signals(bars: Sequence[Bar], n: int) -> list[bool]:
    """`종가 > SMA(N)`. 앞 `n-1`개는 창이 안 차 `False`.

    SMA는 **그날 종가를 포함**하고 체결도 그날 종가다 — 모듈 상단 함정 2가
    말하는 근사다. 미래 봉은 안 본다.
    """
    averages = sma(bars, n)
    return [
        avg is not None and float(bar.close) > avg for bar, avg in zip(bars, averages, strict=True)
    ]


def trend_signals(bars: Sequence[Bar], *, n_in: int, n_out: int) -> list[bool]:
    """진입·청산 이평을 따로 두는 보유 상태 (**2단계**).

    규칙은 대칭 버전과 같은 모양이되 창이 둘이다:

    | | |
    |---|---|
    | 진입 | 안 들고 있는데 `종가 > SMA(N_in)` |
    | 청산 | 들고 있는데 `종가 < SMA(N_out)` |
    | 그 사이 | **상태를 유지한다** |

    🔴 **`sma_signals(n_out)`의 부정이 아니다.** 부정은 `종가 <= SMA`라 같은
    날에도 판다. 두 부등호를 모두 강부등호로 두어야 `N_in == N_out`일 때
    대칭 버전과 정확히 같은 계열이 나온다 — 그게 2단계가 1단계를 포함한다는
    보증이고, 이 함수의 회귀 테스트다.

    앞 `max(N_in, N_out) - 1`개는 두 창 중 하나라도 안 차 있으므로 `False`다.
    창 하나만 찼을 때 진입시키면 `N_out`이 클수록 다른 시점에 시작하게 되어
    **격자 비교가 무의미해진다**(모듈 상단 함정 1과 같은 이유).
    """
    entry_avg = sma(bars, n_in)
    exit_avg = sma(bars, n_out)
    warmup = max(n_in, n_out) - 1
    out = [False] * len(bars)
    holding = False
    for i, bar in enumerate(bars):
        if i < warmup:
            holding = False
            continue
        close = float(bar.close)
        if holding:
            if exit_avg[i] is not None and close < exit_avg[i]:
                holding = False
        elif entry_avg[i] is not None and close > entry_avg[i]:
            holding = True
        out[i] = holding
    return out


def run(
    bars: Sequence[Bar],
    *,
    n: int | None,
    warmup: int,
    initial_cash: int,
    broker_config: SimBrokerConfig | None = None,
    n_out: int | None = None,
) -> TrendRun:
    """한 조건을 재생한다. **`replay()`를 통과한다 — 별도 계산이 아니다.**

    `warmup`은 **격자 전체가 공유하는** 시작 오프셋이다. 각 `N`이 자기
    워밍업만큼만 건너뛰면 창이 달라져 단조성 비교가 무의미해진다(함정 1).

    `n=None`이면 S0 — 워치리스트에 항상 종목이 있어 한 번 사고 끝까지 든다.
    `n_out`을 주면 청산 이평만 따로 간다(2단계) — `trend_signals` 참고.
    """
    if warmup >= len(bars):
        raise ValueError(f"warmup({warmup}) >= 봉 개수({len(bars)}) — 창이 너무 짧다")
    if n is None and n_out is not None:
        raise ValueError("n_out은 n 없이 줄 수 없다 — S0에는 청산 규칙이 없다")

    symbol = bars[0].symbol
    if n is None:
        signals = [True] * len(bars)
    elif n_out is None or n_out == n:
        signals = sma_signals(bars, n)
    else:
        signals = trend_signals(bars, n_in=n, n_out=n_out)
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
        # 종가 체결이라 체결가를 이미 안다 — 증거금 상한(1.30) 대신 여유분만
        # 둔다. 그대로 두면 한 사이클 투입 상한이 76.9%가 되어 100% 노출을
        # 재려던 것이 재지지 않는다.
        diff=DiffConfig(price_limit=CLOSE_FILL_BUFFER),
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
        n_out=n_out,
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


def sweep_exit(
    bars: Sequence[Bar],
    *,
    n_in: int,
    grid: Sequence[int] = N_GRID,
    initial_cash: int,
    broker_config: SimBrokerConfig | None = None,
) -> tuple[TrendRun, list[TrendRun]]:
    """**2단계** — 진입 이평을 `n_in`에 고정하고 청산 이평만 훑는다.

    격자 **전체**를 돈다. `n_out < n_in`(청산을 조이는 쪽)도 뺀 적이 있었는데,
    1차 측정에서 `n_out > n_in`이 오히려 회전을 늘린다는 것이 드러났으므로
    (M008 §4) 어느 방향이 나은지를 **재서** 답한다.

    워밍업은 1단계와 **같은 값**(격자 최대 −1)으로 둔다. 여기서만 줄이면
    1·2단계가 다른 창을 재게 되어 "2단계가 나아졌다"를 말할 수 없다.
    """
    if n_in < 1:
        raise ValueError(f"n_in must be >= 1: {n_in}")
    exits = list(grid)
    if not exits:
        raise ValueError("격자가 비어 있다")

    warmup = max(max(grid), n_in) - 1
    s0 = run(bars, n=None, warmup=warmup, initial_cash=initial_cash, broker_config=broker_config)
    runs = [
        run(
            bars,
            n=n_in,
            n_out=n_out,
            warmup=warmup,
            initial_cash=initial_cash,
            broker_config=broker_config,
        )
        for n_out in exits
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

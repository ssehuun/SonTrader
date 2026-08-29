"""백테스트 러너 (구현 계획 5단계 마지막 조각).

DB에 저장된 일봉(`stock_candles_1d`)과 워치리스트 스냅샷
(`watchlist_snapshots`)을 읽어 하루 1사이클씩 재생하며
`engine/loop.py`의 `run_cycle()`을 반복 호출한다. 실전(`apps/live.py`,
미착수)과 이 러너가 공유하는 것은 `run_cycle` 하나뿐이다 — 여기서 하는
일은 그 하나를 무엇으로 채울지(Context 조립, `SimBroker` 주입, 날짜별
반복)뿐이다.

두 계층으로 나눴다:

- `replay()` — DB를 모른다. 이미 메모리에 있는 데이터(워치리스트/봉/이벤트)로
  사이클을 돈다. 순수하지는 않지만(시뮬레이션 상태를 갖는다) DB 없이
  테스트할 수 있다.
- `run_backtest()` — DB에서 읽어 `replay()`에 넘기는 얇은 배선(wiring) 계층.

## 포지션 다리(bridge)

`SimBroker.positions()`는 브로커가 아는 만큼(수량·평단가)만 돌려준다 —
`core.Position`이 필요로 하는 진입시각·청산조건·event_id는 모른다
(`adapters/broker.py` 참고). 그래서 이 러너는 체결 결과에서 그 정보를
직접 추적해(`_HeldMeta`) 다음 사이클의 `Context.positions`를 조립한다.
이건 사실상 9단계 `engine/reconcile.py`의 축소판이다 — 실전에서는 이
다리 역할을 재구성 가능한 DB `positions` 테이블이 대신하지만, 백테스트는
매번 처음부터 다시 계산하므로 굳이 영속화하지 않는다.

## 명시적으로 확정한 것들

**1. 사이클 날짜는 워치리스트 스냅샷이 있는 날짜다.** 거래일 캘린더 소스는
01문서 §8의 미확정 파라미터라 아직 없다 — `watchlist_snapshots`가 이미
"그날 실제로 파이프라인이 돈 날"의 기록이므로 이를 캘린더 대용으로 쓴다.

**2. 백테스트는 항상 빈 포지션(flat)에서 시작한다.** 웜스타트(이미 보유
중인 포지션에서 시작)는 다루지 않는다 — 필요해지면 그때 추가한다(YAGNI).

**3. `equity`는 현금 + D+2 정산 대기액 + 보유분 시가평가액이다.** 정산
대기액을 목표 비중 산출(`ctx.equity`)에 포함시키는 이유: "5종목 균등
20%"는 총자산 기준 배분 의도이지 유동성 제약이 아니다. 유동성 제약(정산
전 현금 사용 불가)은 `SimBroker`가 별도로 강제한다 — 현금이 모자라는
매수는 **주문 전체를 거부**한다(실전 KIS와 같다). 구조 원칙 그대로
core/전략은 의도를, 어댑터는 실제 제약을 담당한다.

그 결과 **매수 주문의 상당수가 거부된다.** 실측(2026-08-26) 1,466건 중
906건(61.8%)이다. 거부는 실패가 아니라 "미룸"이다 — 다음 사이클에 diff가
같은 차이를 다시 계산해 주문을 재생성하고, 그때는 정산이 풀려 있을 수
있다(`core/diff.py` 상단: "현금 부족은 주문을 줄이는 게 아니라 미루는 문제").
다만 이 비율이 이렇게 높다는 것은 **목표 비중이 가용 현금과 구조적으로
어긋나 있다**는 뜻이고, 그 자체가 `docs/system/02-매매-정교화.md` T21의 근거다.

**4. LLM 판단 계층(6단계)이 없으므로 `judge` 콜백을 주입받는다.**
`Callable[[Event], Judgment | None]` — 지정하지 않으면 아무 이벤트도
진입으로 이어지지 않는다(늘 None). 02문서 5단계 검증 항목인 "규칙 기반
더미 신호로 전체 루프 관통"은 테스트에서 더미 `judge`로 확인한다.

**5. 성과 지표(CAGR/샤프/MDD 등, 01문서 §5.3)는 다루지 않는다.** 이번
슬라이스는 자산 곡선(`equity_curve`)과 체결 로그만 남긴다 — 지표 계산은
별도 슬라이스로 미룬다.

## 분봉 재생 (R2c)

`BarInterval.MINUTE`을 주면 사이클이 **하루 1회가 아니라 봉 1개당 1회**가
된다. 바뀐 것은 이 파일의 배선뿐이다:

| | 일봉 (기본) | 분봉 |
|---|---|---|
| 봉 소스 | `stock_candles_1d` | `stock_candles_1m` (`source='rest'`만) |
| 사이클 시각 | 날짜별 00:00 | 그 구간에 존재하는 **모든 봉 시각**의 합집합 |
| 유니버스 | `watchlist_snapshots` | 호출자가 넘긴 고정 종목 목록 |
| 이벤트 노출 | 그날 첫 사이클에 그날 전부 | `ingested_at` 이후 첫 사이클 |

**`core/`는 한 줄도 바뀌지 않았다.** `core/exit_rules.py`가 이미 봉 주기를
모르고(*"봉의 주기(1분/1일)는 이 모듈이 알지 않는다"*), `InMemoryBarView`는
`datetime` 하나로 자르며, `SimBroker`는 `urgency`와 무관하게 "주문 시각
다음 봉의 시가"에 체결한다 — 그 "다음 봉"이 다음 날 시가에서 다음 분봉으로
바뀌는 것이 전부다. 이 성질이 이 설계의 핵심 자산이므로 깨지 않는다.

### 분봉 유니버스를 왜 호출자가 넘기나

`watchlist_snapshots`는 **12개월 모멘텀 스윙 유니버스**다(1년 합집합 174종목).
데이트레이딩 유니버스는 거래대금 상위 44종목이고 선정 기준이 전혀 다르다.
둘을 섞으면 분봉이 없는 종목을 재생하게 되므로, 분봉 모드는 유니버스를
**명시적 인자로만** 받는다. 넘긴 순서가 곧 `watchlist_ranks`다.

**진입 규칙은 아직 없다.** ORB 설계(R1)가 리서처에게서 넘어오기 전이라,
지금 분봉 모드를 `--entry-trigger watchlist`로 돌리면 "매일 상위 N종목을
산다"는 뜻이 된다 — 배선이 도는지 확인하는 용도이지 전략이 아니다.

### 결손 구간(시장 정지) — T23

봉을 지어내지 않는다. 정지 구간에는 봉이 없고, `SimBroker._next_bar()`가
"다음 봉"을 찾으면 그것이 곧 **재개 후 첫 봉**이라 T23의 체결 규약이 별도
코드 없이 지켜진다.

정지일은 `halted_days`로 남긴다 — 데이트레이딩에서 **당일 청산 전제가 깨지는
날**이고 하락일에 몰려 있어 성과를 분리해서 봐야 한다(`partition_trades()`).

**세션 경계를 박지 않는다.** 판정은 봉 개수가 아니라 **연속성**으로 한다 —
`SessionShape` 참고. 251거래일 중 11일이 09:00~15:30이 아니라서, "정상일은
381봉"으로 재면 그 11일이 전부 틀린다.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from enum import Enum

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.adapters.broker_sim import SimBroker, SimBrokerConfig
from sontrader.core.gate import RejectReason
from sontrader.core.types import (
    Bar,
    Context,
    Event,
    ExitReason,
    ExitRule,
    Fill,
    Judgment,
    OrderStatus,
    Position,
    Side,
)
from sontrader.data import db
from sontrader.engine import fills
from sontrader.engine.context import InMemoryBarView
from sontrader.engine.loop import CycleConfig, Deps, run_cycle

_SYMBOL_CHUNK = 500  # IN 절 바인드 변수 한도(SQLite ~999) 대비 — data/universe.py와 동일한 규약

JudgeFn = Callable[[Event], Judgment | None]

# 시장 정지로 볼 최소 결손 길이. KRX 서킷브레이커는 20분 정지 + 10분 단일가
# 호가접수라 실측 결손이 **30~31분**이고, 종목 고유 VI는 2분이다. 5분은 그
# 사이를 여유 있게 가른다 — 백테스트로 정할 파라미터가 아니라 거래소 규정에서
# 나온 값이라 여기 상수로 둔다.
HALT_GAP = timedelta(minutes=5)


class BarInterval(str, Enum):
    """재생 봉 주기. 사이클 시각과 봉 소스를 **함께** 정한다 — 둘을 따로
    고르게 두면 "일봉으로 분 단위 사이클" 같은 조합이 조용히 만들어진다."""

    DAILY = "1d"
    MINUTE = "1m"


class BacktestError(RuntimeError):
    """백테스트를 돌릴 수 없는 상태 (예: 워치리스트 스냅샷 없음)."""


@dataclass(frozen=True)
class _HeldMeta:
    """브로커가 모르는, 우리 쪽에서만 아는 포지션 부가 정보."""

    entered_at: datetime
    entry_price: float
    exit_rule: ExitRule
    event_id: str | None


@dataclass(frozen=True)
class ClosedTrade:
    """포지션 하나의 생애주기(진입~청산). `apps/report.py`의 성과 지표 계산 단위.

    체결(Fill)은 매수·매도가 별개 레코드라 짝짓기가 필요하다 — 부분체결·
    재진입이 섞이면 fills 목록만으로는 복원할 수 없으므로, 청산이 실제로
    일어나는 시점(`_apply_fills`)에 여기서 직접 짝짓는다.
    """

    symbol: str
    entered_at: datetime
    exit_at: datetime
    entry_price: float
    exit_price: int
    qty: int
    # 왜 팔았는가 (R16). 스톱 청산과 EOD 청산을 못 나누면 무엇이 성과를
    # 만들었는지 알 수 없다 — 예전에는 **청산 시각으로 추정**해야 했다.
    # 목표에서 그냥 빠져 나간 청산(리밸런싱)은 None이다.
    exit_reason: ExitReason | None = None


@dataclass(frozen=True)
class SkippedCandidate:
    """촉발했으나 사지 못한 후보 (리서처 R23 — G4 "촉발의 정보성" 판정 입력).

    백테스트의 대조군 C0에 해당한다. **비용이 0인 표본**이다 — 사지 않았으므로
    수수료도 스프레드도 안 냈고, 그래서 "부등식이 정보를 담았나"를 순수하게
    가를 수 있다. 산 것만 보면 슬롯 배분 규칙의 효과와 구분되지 않는다.

    **`ts`가 날짜가 아니라 시각인 이유**: 예전에는 `(date, Rejection)`으로
    남겼는데, 분봉 재생에서는 하루에 사이클이 380번이라 날짜만으로는 어느
    시점의 거부인지 알 수 없다. 이후 수익률을 재려면 시작점이 필요하다.

    `price`는 그 시각 **마지막 완성 봉의 종가**다. 이후 수익률의 기준점이고,
    없으면(봉이 아직 없음) None이라 그 표본은 G4에서 빠진다.
    """

    ts: datetime
    symbol: str
    reason: RejectReason
    price: int | None
    event_id: str | None = None


@dataclass(frozen=True)
class BacktestResult:
    equity_curve: tuple[tuple[date, int], ...]
    fills: tuple[Fill, ...]
    rejections: tuple[SkippedCandidate, ...]
    closed_trades: tuple[ClosedTrade, ...]
    total_costs: int
    final_cash: int
    final_positions: tuple[Position, ...]
    # 시장 정지가 있었던 날 (분봉 재생에서만 채워진다 — 일봉은 정지를 알 수 없다).
    halted_days: tuple[date, ...] = ()
    # (종목, 날) — **그 종목이 속한 시장**이 멎은 날. 서킷브레이커가 시장별로
    # 발동하므로, KOSPI가 멎은 날 KOSDAQ 종목의 거래는 정지 표본이 아니다.
    halted_symbol_days: frozenset[tuple[str, date]] = frozenset()
    halted_by_market: Mapping[str, tuple[date, ...]] = field(default_factory=dict)
    # 실제로 돈 사이클 수. 일봉은 곧 날짜 수지만 분봉은 다르다 —
    # `equity_curve`는 여전히 하루 한 점이라 이게 없으면 재생 해상도가 안 보인다.
    cycles: int = 0


def partition_trades(
    result: BacktestResult,
) -> tuple[tuple[ClosedTrade, ...], tuple[ClosedTrade, ...]]:
    """(정상일 거래, 정지일에 걸친 거래).

    T23 완료 기준의 "그 표본을 분리해 볼 수 있다"가 이것이다. **진입일이든
    청산일이든 하나라도 정지일이면** 정지 표본으로 분류한다 — 정지는 당일
    청산 전제를 깨뜨리므로 어느 쪽에 걸렸든 그 거래의 성질이 달라진다.

    판정은 **종목 단위**다. 서킷브레이커는 시장별로 발동하므로 KOSPI가 멎은
    날 KOSDAQ 종목의 거래는 정지 표본이 아니다(`halt_report()`).
    """
    halted = result.halted_symbol_days
    if not halted:
        return result.closed_trades, ()

    def touched(trade: ClosedTrade) -> bool:
        return (trade.symbol, trade.entered_at.date()) in halted or (
            trade.symbol,
            trade.exit_at.date(),
        ) in halted

    normal = tuple(t for t in result.closed_trades if not touched(t))
    affected = tuple(t for t in result.closed_trades if touched(t))
    return normal, affected


def exit_reason_breakdown(
    trades: Sequence[ClosedTrade],
) -> dict[str, tuple[int, float, float]]:
    """청산 사유 → (건수, 승률, 평균 수익률). 비용 **전** 총수익 기준이다.

    리서처 R16이 요구하는 분해. 이게 없으면 성과를 만든 것이 스톱인지 EOD인지
    알 수 없고, 실제로 **청산 시각으로 추정**해야 했다 — 15:00에 몰린 청산을
    보고 "아마 EOD겠지" 하는 식이라 스톱과 EOD가 같은 분에 겹치면 갈리지 않는다.
    """
    buckets: dict[str, list[float]] = {}
    for trade in trades:
        key = trade.exit_reason.value if trade.exit_reason is not None else "rebalance"
        buckets.setdefault(key, []).append(
            (trade.exit_price - trade.entry_price) / trade.entry_price
        )
    return {
        key: (
            len(returns),
            sum(1 for r in returns if r > 0) / len(returns),
            sum(returns) / len(returns),
        )
        for key, returns in sorted(buckets.items())
    }


def run_backtest(
    engine: Engine,
    *,
    start: date,
    end: date,
    initial_cash: int,
    judge: JudgeFn | None = None,
    broker_config: SimBrokerConfig | None = None,
    cycle_config: CycleConfig | None = None,
    interval: BarInterval = BarInterval.DAILY,
    symbols: Sequence[str] | None = None,
) -> BacktestResult:
    """DB에서 읽어 `replay()`에 넘긴다.

    `interval=MINUTE`이면 `symbols`가 **필수**다 — 분봉 유니버스는
    `watchlist_snapshots`(모멘텀 스윙)에서 나오지 않는다(모듈 상단 참고).
    넘긴 순서가 곧 `watchlist_ranks`이므로 유동성 순으로 넘긴다.
    """
    if interval is BarInterval.MINUTE:
        return _run_minute_backtest(
            engine,
            start=start,
            end=end,
            initial_cash=initial_cash,
            judge=judge,
            broker_config=broker_config,
            cycle_config=cycle_config,
            symbols=symbols,
        )

    watchlists, watchlist_ranks = _load_watchlists(engine, start, end)
    if not watchlists:
        raise BacktestError(
            f"no watchlist snapshots between {start} and {end} — "
            "run `sontrader build-universe` first"
        )
    universe = sorted(
        {symbol for symbols_on_day in watchlists.values() for symbol in symbols_on_day}
    )
    bars = _load_bars(engine, universe, start, end)
    events = _load_events(engine, universe, start, end)
    trading_units = _load_trading_units(engine, universe)
    return replay(
        watchlists=watchlists,
        bars=bars,
        events=events,
        initial_cash=initial_cash,
        judge=judge,
        broker_config=broker_config,
        cycle_config=cycle_config,
        trading_units=trading_units,
        watchlist_ranks=watchlist_ranks,
    )


def _run_minute_backtest(
    engine: Engine,
    *,
    start: date,
    end: date,
    initial_cash: int,
    judge: JudgeFn | None,
    broker_config: SimBrokerConfig | None,
    cycle_config: CycleConfig | None,
    symbols: Sequence[str] | None,
) -> BacktestResult:
    if not symbols:
        raise BacktestError(
            "minute replay needs an explicit universe — pass `symbols` "
            "(watchlist_snapshots is the momentum swing universe, not the "
            "day-trading one; see module docstring)"
        )
    ordered = list(dict.fromkeys(symbols))  # 순서 유지 + 중복 제거 (순서가 곧 rank)
    bars = _load_minute_bars(engine, ordered, start, end)
    cycle_times = _minute_cycle_times(bars)
    if not cycle_times:
        raise BacktestError(
            f"no minute bars for {ordered} between {start} and {end} — "
            "run `sontrader collect-minutes` first"
        )
    days = sorted({ts.date() for ts in cycle_times})
    # 분봉 모드의 워치리스트는 매일 같은 고정 유니버스다. 날짜별로 다르게
    # 두려면 그건 유니버스 선정 규칙이고, 그건 리서처의 몫이다(R1).
    watchlists = {day: ordered for day in days}
    ranks = {symbol: index + 1 for index, symbol in enumerate(ordered)}
    watchlist_ranks = {day: ranks for day in days}

    events = _load_events(engine, ordered, start, end)
    trading_units = _load_trading_units(engine, ordered)
    halts = halt_report(bars, _load_markets(engine, ordered))
    # 세션 경계는 **그날 실제 봉에서 유도한다** — 시각을 박지 않는다(R12).
    shapes = session_shapes(bars)
    return replay(
        watchlists=watchlists,
        bars=bars,
        events=events,
        initial_cash=initial_cash,
        judge=judge,
        broker_config=broker_config,
        cycle_config=cycle_config,
        trading_units=trading_units,
        watchlist_ranks=watchlist_ranks,
        cycle_times=cycle_times,
        halts=halts,
        shapes=shapes,
    )


def replay(
    *,
    watchlists: Mapping[date, Sequence[str]],
    bars: Mapping[str, Sequence[Bar]],
    events: Mapping[date, Sequence[Event]],
    initial_cash: int,
    judge: JudgeFn | None = None,
    broker_config: SimBrokerConfig | None = None,
    cycle_config: CycleConfig | None = None,
    trading_units: Mapping[str, int] | None = None,
    watchlist_ranks: Mapping[date, Mapping[str, int]] | None = None,
    cycle_times: Sequence[datetime] | None = None,
    halts: HaltReport | None = None,
    shapes: Mapping[date, SessionShape] | None = None,
) -> BacktestResult:
    """사이클을 돈다. DB를 모른다.

    `cycle_times`를 주면 그 시각들로 사이클을 돈다(분봉 재생). 주지 않으면
    예전 그대로 **날짜별 00:00 한 번**이다 — 일봉 기준선이 이 변경으로
    움직이면 안 되므로, 기본 경로는 한 글자도 달라지지 않게 두었다.
    """
    dates = sorted(watchlists)
    if not dates:
        raise BacktestError("no cycle dates to replay")
    shapes = shapes or {}
    if cycle_times is None:
        cycles = [datetime.combine(day, time.min) for day in dates]
        gate_events_by_ingested_at = False
    else:
        cycles = sorted(cycle_times)
        if not cycles:
            raise BacktestError("no cycle times to replay")
        # 하루 안에서 여러 번 도는 순간 "그날 이벤트 전부를 00:00에 본다"는
        # 일봉의 근사가 명백한 look-ahead가 된다 — 15:00에 들어온 공시로
        # 09:01에 매수하게 된다. 분봉에서는 `ingested_at` 이후 첫 사이클에만
        # 노출한다(실전 `engine/live_context.py`와 같은 경계).
        gate_events_by_ingested_at = True

    # 백테스트는 사람이 없다 — 결정적이어야 하므로 승인 큐를 거치면 안 된다
    # (engine/loop.py 참고). 호출자가 별도 cycle_config를 넘겨도 이 값은
    # 유지한다: 백테스트에서 승인 큐를 켜면 Deps.engine이 없어 즉시 에러이거나,
    # 있어도 사람 없이는 영원히 승인되지 않아 매매가 멈춘다.
    cycle_config = cycle_config or CycleConfig()
    if cycle_config.check_killswitch:
        cycle_config = replace(cycle_config, check_killswitch=False)

    judge = judge or (lambda _event: None)
    units = dict(trading_units or {})
    ranks_by_day = watchlist_ranks or {}
    bar_view = InMemoryBarView(bars)
    broker = SimBroker(bars, initial_cash=initial_cash, config=broker_config)
    deps = Deps(broker=broker)

    held: dict[str, _HeldMeta] = {}
    used_event_ids: set[str] = set()
    last_exit_at: dict[str, datetime] = {}

    # 자산 곡선은 **하루 한 점**으로 유지한다 (`apps/report.py`의 CAGR·MDD가
    # 날짜 간격에 기대고, 분봉이면 점이 95,000개가 된다). 그날 마지막 사이클의
    # 값이 남는다 — 일봉 모드는 사이클이 하루 하나라 예전과 동일하다.
    equity_by_day: dict[date, int] = {}
    fills: list[Fill] = []
    rejections: list[SkippedCandidate] = []
    closed_trades: list[ClosedTrade] = []

    events_by_cycle = _events_by_cycle(events, cycles, gate=gate_events_by_ingested_at)
    remaining_by_cycle = _session_bars_remaining(cycles, shapes)

    for now in cycles:
        day = now.date()
        view = bar_view.at(now)
        broker_positions = {p.symbol: p for p in broker.positions()}

        cash = broker.cash()
        mark_to_market = sum(
            bp.qty * (bar.close if (bar := view.latest(symbol)) is not None else bp.avg_price)
            for symbol, bp in broker_positions.items()
        )
        equity = int(cash + broker.pending_settlement + mark_to_market)

        day_events = events_by_cycle.get(now, ())
        judgments = {
            event.event_id: verdict for event in day_events if (verdict := judge(event)) is not None
        }

        ctx = Context(
            now=now,
            bars=view,
            watchlist=tuple(watchlists.get(day, ())),
            positions=_reconstruct_positions(broker_positions, held),
            new_events=day_events,
            judgments=judgments,
            cash=cash,
            equity=equity,
            used_event_ids=frozenset(used_event_ids),
            last_exit_at=dict(last_exit_at),
            trading_units=units,
            watchlist_ranks=ranks_by_day.get(day, {}),
            session_bars_remaining=remaining_by_cycle.get(now),
        )

        result = run_cycle(ctx, deps, cycle_config)
        # `broker_positions`는 이 사이클 체결을 반영하기 **전**의 잔고다
        # (루프 맨 위에서 찍었다). `engine/fills.py`가 "포지션이 비었는가"를
        # 판정하려면 이 시점의 수량이 필요하다.
        _apply_fills(
            result,
            held,
            used_event_ids,
            last_exit_at,
            fills,
            closed_trades,
            held_qty={symbol: bp.qty for symbol, bp in broker_positions.items()},
        )

        # 거부 시점의 가격을 함께 남긴다 — 이후 수익률의 기준점이 없으면
        # G4를 못 잰다. `view`는 이 사이클의 look-ahead 차단된 뷰다.
        rejections.extend(
            SkippedCandidate(
                ts=now,
                symbol=r.symbol,
                reason=r.reason,
                price=bar.close if (bar := view.latest(r.symbol)) is not None else None,
                event_id=r.event_id,
            )
            for r in result.rejections
        )
        equity_by_day[day] = equity

    final_positions = _reconstruct_positions({p.symbol: p for p in broker.positions()}, held)
    return BacktestResult(
        equity_curve=tuple(sorted(equity_by_day.items())),
        fills=tuple(fills),
        rejections=tuple(rejections),
        closed_trades=tuple(closed_trades),
        total_costs=broker.total_costs,
        final_cash=broker.cash(),
        final_positions=final_positions,
        halted_days=halts.days if halts else (),
        halted_symbol_days=halts.symbol_days if halts else frozenset(),
        halted_by_market=dict(halts.by_market) if halts else {},
        cycles=len(cycles),
    )


def _reconstruct_positions(
    broker_positions: Mapping[str, object], held: Mapping[str, _HeldMeta]
) -> tuple[Position, ...]:
    """브로커 잔고 + 우리 쪽 메타데이터 → 전략이 볼 `Position` 목록.

    **고아 포지션을 조용히 넘기지 않는다.** 브로커에는 수량이 있는데 `held`에
    진입 정보가 없으면, 그 종목은 전략에게 보이지 않으면서 실제로는 보유
    중이다 — 청산 규칙이 영영 안 걸리고, 게이트의 슬롯 계산에서도 빠져
    상한이 무너진다. 2026-08-26에 발견된 부기 버그가 정확히 이 상태를
    만들었고, 그 결과 전체 사이클의 94.9%에서 동시보유 상한 5가 깨져 있었다.
    성과 수치가 통째로 무효가 될 만큼 비싼 버그였으므로, 되돌아오면
    **즉시 터지게** 한다.
    """
    orphans = sorted(set(broker_positions) - set(held))
    if orphans:
        raise ValueError(
            f"broker holds {orphans} but no entry metadata is tracked — "
            "orphaned position (see engine/fills.py rule 2)"
        )

    positions = []
    for symbol, meta in held.items():
        bp = broker_positions.get(symbol)
        if bp is None:
            continue
        positions.append(
            Position(
                symbol=symbol,
                qty=bp.qty,
                avg_price=bp.avg_price,
                entered_at=meta.entered_at,
                exit_rule=meta.exit_rule,
                event_id=meta.event_id,
            )
        )
    return tuple(positions)


def _apply_fills(
    result,
    held,
    used_event_ids,
    last_exit_at,
    fills_out,
    closed_trades_out,
    *,
    held_qty: Mapping[str, int],
) -> None:
    """체결을 메모리 부기에 반영한다.

    포지션 변경 판정 자체는 `engine/fills.py`가 한다 — 실전(`apps/live.py`)이
    같은 규칙을 DB에 반영하므로, 규칙을 여기 복제하면 두 경로가 갈라진다.
    여기서는 백테스트에만 필요한 것(체결 로그, 종료된 거래 기록)을 더한다.

    `held_qty`는 이번 체결 **전**의 보유 수량이다. 트림(부분 매도)을 청산으로
    오인하지 않으려면 잔량이 필요하다 — 오인하면 여기서 `held.pop()`이 돌아
    브로커에 남은 수량이 전략에게 보이지 않는 고아가 된다.
    """
    for order_result in result.order_results:
        if order_result.status in (OrderStatus.FILLED, OrderStatus.PARTIAL):
            fills_out.extend(order_result.fills)
            if order_result.order.side is Side.BUY and order_result.order.event_id is not None:
                used_event_ids.add(order_result.order.event_id)

    for change in fills.position_changes(result.order_results, held=held_qty):
        if isinstance(change, fills.Opened):
            held[change.symbol] = _HeldMeta(
                change.entered_at, change.entry_price, change.exit_rule, change.event_id
            )
            continue

        meta = held.pop(change.symbol, None)
        last_exit_at[change.symbol] = change.exited_at
        if meta is None:
            # 브로커가 보유분을 청산했는데 우리 쪽 부기에 진입 정보가 없다 —
            # 어딘가에서 상태가 어긋난 것이므로 조용히 넘기지 않는다.
            raise ValueError(f"closing {change.symbol!r} but no entry metadata was tracked")
        closed_trades_out.append(
            ClosedTrade(
                symbol=change.symbol,
                entered_at=meta.entered_at,
                exit_at=change.exited_at,
                entry_price=meta.entry_price,
                exit_price=change.exit_price,
                qty=change.qty,
                exit_reason=change.exit_reason,
            )
        )


def _events_by_cycle(
    events: Mapping[date, Sequence[Event]],
    cycles: Sequence[datetime],
    *,
    gate: bool,
) -> dict[datetime, tuple[Event, ...]]:
    """이벤트를 사이클 시각에 배정한다. 한 이벤트는 정확히 한 번만 노출된다.

    `gate=False` (일봉): **그날 첫 사이클에 그날 것 전부**. 예전 그대로다 —
    사이클이 하루 한 번뿐이라 그 안에서 더 잘게 나눌 수 없다. 15:00에 들어온
    공시로 그날 00:00에 판단하는 셈이라 이미 낙관적이지만, 그 근사 위에서
    기존 기준선이 측정됐으므로 여기서 조용히 바꾸지 않는다.

    `gate=True` (분봉): **`ingested_at` 이후 첫 사이클**. 그날 마지막 봉보다
    늦게 들어온 공시는 자연히 다음 거래일 첫 사이클로 밀린다(bisect가 전체
    사이클 목록을 훑으므로 날짜 경계를 알 필요가 없다). 마지막 사이클보다
    늦으면 재생 구간 안에서 볼 기회가 없었다는 뜻이라 버린다.
    """
    result: dict[datetime, list[Event]] = {}
    if not gate:
        first_of_day: dict[date, datetime] = {}
        for ts in cycles:
            first_of_day.setdefault(ts.date(), ts)
        for day, day_events in events.items():
            cycle = first_of_day.get(day)
            if cycle is not None:
                result.setdefault(cycle, []).extend(day_events)
        return {ts: tuple(rows) for ts, rows in result.items()}

    for day_events in events.values():
        for event in day_events:
            idx = bisect.bisect_left(cycles, event.ingested_at)
            if idx >= len(cycles):
                continue  # 재생 구간이 끝난 뒤에 들어온 공시 — 볼 기회가 없었다
            result.setdefault(cycles[idx], []).append(event)
    return {ts: tuple(sorted(rows, key=lambda e: e.ingested_at)) for ts, rows in result.items()}


def _session_bars_remaining(
    cycles: Sequence[datetime], shapes: Mapping[date, SessionShape]
) -> dict[datetime, int]:
    """사이클 시각 → 그 세션에 **남은 연속거래 봉 수** (`ExitRule.eod_exit_bars`의 입력).

    `SessionShape.close_ts`(그날 연속거래 마지막 봉)까지만 센다 — 마감 단일가
    봉은 빼는데, 그게 리서처 R12의 "마지막 **연속거래** 봉" 문구이기도 하고
    단일가 체결을 일반 봉처럼 쓰는 것이 아직 미결(R20)이기 때문이다.

    `shapes`가 없는 날은 값을 넣지 않는다(→ `None` → EOD 청산 미발동).
    일봉 재생이 그 경로다.

    **근사 하나**: 사이클 시각은 유니버스 **전체**의 합집합이라, 시장 정지가
    있던 날에는 멎지 않은 시장의 봉이 섞여 남은 봉 수가 실제보다 많게 나온다.
    정지일은 어차피 성과에서 분리해 보는 표본이므로(T23) 그대로 둔다.
    """
    remaining: dict[datetime, int] = {}
    by_day: dict[date, list[datetime]] = {}
    for ts in cycles:
        by_day.setdefault(ts.date(), []).append(ts)

    for day, stamps in by_day.items():
        shape = shapes.get(day)
        if shape is None:
            continue
        # 연속거래 구간에 속하는 사이클만 센다. `close_ts` 이후(단일가 봉)는
        # 0으로 둔다 — 남은 봉이 없다는 뜻이고, 그 시점에 EOD가 안 걸렸다면
        # 이미 늦었다.
        tradable = [ts for ts in stamps if ts <= shape.close_ts]
        total = len(tradable)
        for index, ts in enumerate(tradable):
            remaining[ts] = total - index - 1
        for ts in stamps:
            remaining.setdefault(ts, 0)
    return remaining


def _minute_cycle_times(bars: Mapping[str, Sequence[Bar]]) -> list[datetime]:
    """사이클 시각 = 유니버스에 **실제로 존재하는** 봉 시각의 합집합.

    빈 봉을 지어내지 않는다(T23) — 시장 정지 구간에는 사이클 자체가 없다.
    그래서 정지 중에 발동한 청산 신호는 다음 사이클, 즉 재개 후 첫 봉에서
    비로소 주문이 나가고 `SimBroker`가 그 다음 봉의 시가에 체결한다.
    """
    return sorted({bar.ts for rows in bars.values() for bar in rows})


@dataclass(frozen=True)
class SessionShape:
    """그날 세션의 실제 모양. **전부 데이터에서 유도한다 — 시각을 박지 않는다.**

    "정상일은 381봉(09:00~15:19 + 15:30)"이라는 판정을 쓰다가 버렸다. 실측
    251거래일 중 **11일이 09:00~15:30이 아니다**(4.4%):

    | 모양 | 날 | 예 |
    |---|---|---|
    | 마감 단일가가 15:32에 한 번 더 | 8일 | 2025-10-02, 2026-08-13 |
    | 세션 전체가 1시간 밀림 (10:00~16:30) | 1일 | 2025-11-13 (수능) |
    | 10:00 개장 | 1일 | 2026-01-02 (개장식) |
    | 08:31 장전 봉 | 1일 | 2026-08-20 |

    봉 **개수**로 판정하면 이 11일이 전부 틀린다. 그래서 개수가 아니라
    **연속성**을 본다 — 봉 간격이 벌어진 자리를 찾고, 그 자리가 연속거래
    구간의 **안쪽**인지 **바깥쪽**인지로 가른다.

    - 바깥쪽(앞뒤 꼬리) = 단일가 봉. 정상이다
    - 안쪽 = 거래가 멎었다는 뜻. `HALT_GAP` 이상이면 시장 정지다

    이 성질은 시각을 하나도 모르고 성립하므로 개장·폐장이 밀려도 깨지지
    않는다. 리서처 R12("마지막 연속거래 봉의 20분 전")가 요구하는 세션 경계도
    `open_ts`/`close_ts`가 그대로 답이다.
    """

    day: date
    open_ts: datetime  # 연속거래 첫 봉
    close_ts: datetime  # 연속거래 마지막 봉
    pre_auction: tuple[datetime, ...]  # 연속거래 **앞**에 떨어져 있는 봉 (장전 단일가)
    post_auction: tuple[datetime, ...]  # **뒤**에 떨어져 있는 봉 (마감 단일가 15:30·15:32)
    halt_gaps: tuple[tuple[datetime, datetime], ...]  # 연속거래 구간 **안**의 결손
    bars: int

    @property
    def halted(self) -> bool:
        return bool(self.halt_gaps)


def session_shapes(bars: Mapping[str, Sequence[Bar]]) -> dict[date, SessionShape]:
    """날짜 → 그날 세션 모양. 넘긴 종목들의 **합집합**으로 본다.

    한 종목만 잃은 분은 그 종목 사정(VI, 거래정지)이고 합집합에서 메워진다.
    실측으로 실제 갈렸다: 2026-03-05는 005930만 379봉, 나머지 종목은
    381봉이었다(종목 고유 VI).

    **넘기는 종목은 한 시장 안에서만 골라야 한다** — 서킷브레이커가 시장별로
    발동하기 때문이다(`halt_report()` 참고). 시장을 섞어 넘기면 정지가 메워진다.
    """
    per_day: dict[date, set[datetime]] = {}
    for rows in bars.values():
        for bar in rows:
            per_day.setdefault(bar.ts.date(), set()).add(bar.ts)
    return {day: _session_shape(day, stamps) for day, stamps in per_day.items()}


@dataclass(frozen=True)
class HaltReport:
    """시장 정지 계측 (T23 / 리서처 R17)."""

    days: tuple[date, ...]  # 어느 시장이든 정지가 있었던 날
    by_market: Mapping[str, tuple[date, ...]]  # 시장 → 그 시장이 멎은 날
    symbol_days: frozenset[tuple[str, date]]  # (종목, 날) — 그 종목의 시장이 멎은 날


def halt_report(bars: Mapping[str, Sequence[Bar]], markets: Mapping[str, str]) -> HaltReport:
    """**서킷브레이커는 시장별로 발동한다.** 그래서 시장을 나눠서 판정한다.

    유니버스 전체를 한 덩어리로 보면 정지가 통째로 사라진다. 실측
    (2026-08-27, 거래대금 상위 37종목 / 2025-09-01~2026-08-21):

    | | 정지일 |
    |---|---|
    | 유니버스 전체를 합집합 | **2일** |
    | KOSPI / KOSDAQ 나눠서 | **9일** |

    2026-07-28 10:13~10:43에 KOSPI가 멎는 동안 KOSDAQ 6종목(010170·036930·
    080220·196170·240810·403870)은 **29분 내내 거래됐다.** 합집합에는 구멍이
    없으니 정지가 없던 것처럼 보인다. 2026-03-04는 두 시장이 다 멎었지만
    시각이 어긋나서(KOSDAQ은 그 구간에 2~3봉) 역시 메워졌다.

    `markets`에 없는 종목은 `"?"` 그룹으로 묶는다 — 마스터에 없는 종목
    (상장폐지 등)을 조용히 버리면 그 종목의 정지일이 사라진다.

    **한계**: 한 시장에 종목이 하나뿐이면 그 종목의 VI가 시장 정지로 잡힌다.
    유니버스가 그 시장을 대표할 만큼 있어야 의미가 있다.
    """
    grouped: dict[str, dict[str, Sequence[Bar]]] = {}
    for symbol, rows in bars.items():
        grouped.setdefault(markets.get(symbol) or "?", {})[symbol] = rows

    by_market: dict[str, tuple[date, ...]] = {}
    symbol_days: set[tuple[str, date]] = set()
    all_days: set[date] = set()
    for market, group in grouped.items():
        halted = _halted_days(group)
        if not halted:
            continue
        by_market[market] = halted
        all_days.update(halted)
        symbol_days.update((symbol, day) for symbol in group for day in halted)

    return HaltReport(
        days=tuple(sorted(all_days)),
        by_market=by_market,
        symbol_days=frozenset(symbol_days),
    )


def _session_shape(day: date, stamps: set[datetime]) -> SessionShape:
    ordered = sorted(stamps)
    if len(ordered) == 1:
        only = ordered[0]
        return SessionShape(day, only, only, (), (), (), 1)

    gaps = [ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1)]
    # 봉 주기도 유도한다 — 1분봉이라고 가정하지 않는다. 정상 구간이 가장
    # 촘촘하므로 최소 간격이 곧 봉 주기다.
    step = min(gaps)

    adjacent = [i for i, gap in enumerate(gaps) if gap == step]
    if not adjacent:
        # 연속한 봉이 한 쌍도 없다 — 연속거래 구간을 특정할 수 없으므로
        # 전체를 그대로 두고 판단을 보류한다(정지로 단정하지 않는다).
        return SessionShape(day, ordered[0], ordered[-1], (), (), (), len(ordered))

    lo, hi = adjacent[0], adjacent[-1] + 1  # 연속거래 구간 = ordered[lo:hi+1]
    halt_gaps = tuple((ordered[i], ordered[i + 1]) for i in range(lo, hi) if gaps[i] >= HALT_GAP)
    return SessionShape(
        day=day,
        open_ts=ordered[lo],
        close_ts=ordered[hi],
        pre_auction=tuple(ordered[:lo]),
        post_auction=tuple(ordered[hi + 1 :]),
        halt_gaps=halt_gaps,
        bars=len(ordered),
    )


def _halted_days(bars: Mapping[str, Sequence[Bar]]) -> tuple[date, ...]:
    """한 시장 안에서의 정지일. 시장이 섞여 있으면 `halt_report()`를 쓴다.

    **알고 남긴 한계**: 정지가 개장 직후나 마감 직전에 걸려 연속거래 구간의
    끝에 붙으면, 그 결손이 단일가 꼬리와 구분되지 않아 놓친다. 실측 1년
    251거래일에서는 그런 날이 없었다(정지 9일 전부 장중이다).
    """
    return tuple(sorted(day for day, shape in session_shapes(bars).items() if shape.halted))


# --- DB 로딩 ---------------------------------------------------------------


def _load_watchlists(
    engine: Engine, start: date, end: date
) -> tuple[dict[date, list[str]], dict[date, dict[str, int]]]:
    """(날짜 → 순위순 종목, 날짜 → 종목별 **저장된** rank).

    rank를 따로 돌려주는 이유: 위치+1은 저장된 순위가 아니다. 히스테리시스
    (30/42)로 순위에 구멍이 뚫려 있어 실측 1,864일 중 1,793일이 1..N 연속이
    아니다 (`core.types.Context.watchlist_ranks` 참고).
    """
    columns = db.watchlist_snapshots.c
    result: dict[date, list[str]] = {}
    ranks: dict[date, dict[str, int]] = {}
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(columns.date, columns.symbol, columns.rank)
            .where(columns.date >= start, columns.date <= end)
            .order_by(columns.date, columns.rank)
        )
        for day, symbol, rank in rows:
            result.setdefault(day, []).append(symbol)
            ranks.setdefault(day, {})[symbol] = rank
    return result, ranks


def _load_bars(
    engine: Engine, symbols: Sequence[str], start: date, end: date
) -> dict[str, list[Bar]]:
    result: dict[str, list[Bar]] = {symbol: [] for symbol in symbols}
    if not symbols:
        return result
    columns = db.stock_candles_1d.c
    with engine.connect() as conn:
        for chunk_start in range(0, len(symbols), _SYMBOL_CHUNK):
            chunk = symbols[chunk_start : chunk_start + _SYMBOL_CHUNK]
            rows = conn.execute(
                sa.select(
                    columns.symbol,
                    columns.date,
                    columns.open,
                    columns.high,
                    columns.low,
                    columns.close,
                    columns.volume,
                )
                .where(columns.symbol.in_(chunk), columns.date >= start, columns.date <= end)
                .order_by(columns.symbol, columns.date)
            )
            for symbol, day, o, h, low, c, v in rows:
                if None in (o, h, low, c):
                    continue  # 결손 봉 — 있는 만큼만 쓴다
                result[symbol].append(
                    Bar(
                        symbol=symbol,
                        ts=datetime.combine(day, time.min),
                        open=o,
                        high=h,
                        low=low,
                        close=c,
                        volume=v or 0,
                    )
                )
    return result


def _load_minute_bars(
    engine: Engine, symbols: Sequence[str], start: date, end: date
) -> dict[str, list[Bar]]:
    """1분봉. **`source='rest'`만 읽는다** (`data/db.py`의 규약 / R8).

    웹소켓 집계 봉(`ws`)에는 시간외 거래가 섞여 있어(실측: 15:58 봉까지)
    그것으로 학습하면 실전에서 재현 불가능한 청산이 나온다 — 그 시간에는
    시장가 즉시 청산이 안 된다.

    **허용 목록으로 거른다 (`== 'rest'`), 배제 목록이 아니다 (`!= 'ws'`).**
    이 구분이 실제로 갈랐다 — 실측된 유령봉 13행은 `source`가 `'ws'`가 아니라
    **NULL**이었다(`source` 컬럼이 생기기 전에 쓰인 레거시 행). `!= 'ws'`로
    걸렀다면 SQL의 NULL 비교 때문에 그대로 통과했을 것이다. 새 출처가
    생기더라도 명시적으로 허용하기 전까지는 백테스트에 안 들어온다.

    가격은 **원주가**다 (분봉 API에 수정주가 파라미터가 없다). 일봉 백테스트는
    수정주가라 두 모드의 가격 기준이 기업행위 구간에서 어긋난다 — R2 격차 5번,
    `docs/system/02-매매-정교화.md` T9.
    """
    result: dict[str, list[Bar]] = {symbol: [] for symbol in symbols}
    if not symbols:
        return result
    columns = db.stock_candles_1m.c
    start_dt = datetime.combine(start, time.min)
    end_dt = datetime.combine(end, time.max)
    with engine.connect() as conn:
        for chunk_start in range(0, len(symbols), _SYMBOL_CHUNK):
            chunk = symbols[chunk_start : chunk_start + _SYMBOL_CHUNK]
            rows = conn.execute(
                sa.select(
                    columns.symbol,
                    columns.ts,
                    columns.open,
                    columns.high,
                    columns.low,
                    columns.close,
                    columns.volume,
                )
                .where(
                    columns.symbol.in_(chunk),
                    columns.source == "rest",
                    columns.ts >= start_dt,
                    columns.ts <= end_dt,
                )
                .order_by(columns.symbol, columns.ts)
            )
            for symbol, ts, o, h, low, c, v in rows:
                result[symbol].append(
                    Bar(symbol=symbol, ts=ts, open=o, high=h, low=low, close=c, volume=v or 0)
                )
    return result


def _load_markets(engine: Engine, symbols: Sequence[str]) -> dict[str, str]:
    """종목 → 시장(KOSPI/KOSDAQ). 서킷브레이커가 시장별로 발동하므로 정지
    판정에 필요하다(`halt_report()`).

    `_load_trading_units`와 같은 한계를 진다 — `symbol_master`는 일별 이력이
    없어(T12) 과거 구간에도 오늘의 소속을 쓴다. 이전(코스닥→코스피)이 있었다면
    그 구간의 정지 판정이 틀린다. 드물고, 성과가 아니라 표본 분류에만 쓰인다.
    """
    result: dict[str, str] = {}
    if not symbols:
        return result
    columns = db.symbol_master.c
    with engine.connect() as conn:
        for chunk_start in range(0, len(symbols), _SYMBOL_CHUNK):
            chunk = symbols[chunk_start : chunk_start + _SYMBOL_CHUNK]
            rows = conn.execute(
                sa.select(columns.symbol, columns.market).where(columns.symbol.in_(chunk))
            )
            for symbol, market in rows:
                if market:
                    result[symbol] = market
    return result


def _load_trading_units(engine: Engine, symbols: Sequence[str]) -> dict[str, int]:
    """종목 → 매매수량단위.

    **오늘의 마스터 스냅샷이다.** `symbol_master`는 일별 이력이 없어(T12)
    2019년 백테스트에도 2026년 값을 쓴다 — 과거에 단위가 달랐다면 그 구간은
    틀린다. 생존 편향과 같은 뿌리의 한계라 T12가 풀려야 정확해진다.
    지금은 전 종목이 1이라 실질 차이가 없다(2026-08-26 실측).
    """
    result: dict[str, int] = {}
    if not symbols:
        return result
    columns = db.symbol_master.c
    with engine.connect() as conn:
        for chunk_start in range(0, len(symbols), _SYMBOL_CHUNK):
            chunk = symbols[chunk_start : chunk_start + _SYMBOL_CHUNK]
            rows = conn.execute(
                sa.select(columns.symbol, columns.trading_unit).where(columns.symbol.in_(chunk))
            )
            for symbol, unit in rows:
                if unit is not None and unit >= 1:
                    result[symbol] = unit
    return result


def _load_events(
    engine: Engine, symbols: Sequence[str], start: date, end: date
) -> dict[date, list[Event]]:
    result: dict[date, list[Event]] = {}
    if not symbols:
        return result
    columns = db.events.c
    start_dt = datetime.combine(start, time.min)
    end_dt = datetime.combine(end, time.max)
    with engine.connect() as conn:
        for chunk_start in range(0, len(symbols), _SYMBOL_CHUNK):
            chunk = symbols[chunk_start : chunk_start + _SYMBOL_CHUNK]
            rows = conn.execute(
                sa.select(
                    columns.event_id,
                    columns.symbol,
                    columns.corp_code,
                    columns.event_type,
                    columns.norm_key,
                    columns.title,
                    columns.published_at,
                    columns.ingested_at,
                )
                .where(
                    columns.symbol.in_(chunk),
                    columns.ingested_at >= start_dt,
                    columns.ingested_at <= end_dt,
                )
                .order_by(columns.ingested_at)
            )
            for row in rows:
                event = Event(
                    event_id=row.event_id,
                    symbol=row.symbol,
                    corp_code=row.corp_code,
                    event_type=row.event_type,
                    norm_key=row.norm_key,
                    title=row.title,
                    published_at=row.published_at,
                    ingested_at=row.ingested_at,
                )
                result.setdefault(row.ingested_at.date(), []).append(event)
    return result

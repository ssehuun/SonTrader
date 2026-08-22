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
전 현금 사용 불가)은 이미 `SimBroker`의 매수 클램핑이 별도로 강제한다
(구조 원칙 — core/전략은 의도를, 어댑터는 실제 제약을 담당).

**4. LLM 판단 계층(6단계)이 없으므로 `judge` 콜백을 주입받는다.**
`Callable[[Event], Judgment | None]` — 지정하지 않으면 아무 이벤트도
진입으로 이어지지 않는다(늘 None). 02문서 5단계 검증 항목인 "규칙 기반
더미 신호로 전체 루프 관통"은 테스트에서 더미 `judge`로 확인한다.

**5. 성과 지표(CAGR/샤프/MDD 등, 01문서 §5.3)는 다루지 않는다.** 이번
슬라이스는 자산 곡선(`equity_curve`)과 체결 로그만 남긴다 — 지표 계산은
별도 슬라이스로 미룬다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.adapters.broker_sim import SimBroker, SimBrokerConfig
from sontrader.core.gate import Rejection
from sontrader.core.types import (
    Bar,
    Context,
    Event,
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


@dataclass(frozen=True)
class BacktestResult:
    equity_curve: tuple[tuple[date, int], ...]
    fills: tuple[Fill, ...]
    rejections: tuple[tuple[date, Rejection], ...]
    closed_trades: tuple[ClosedTrade, ...]
    total_costs: int
    final_cash: int
    final_positions: tuple[Position, ...]


def run_backtest(
    engine: Engine,
    *,
    start: date,
    end: date,
    initial_cash: int,
    judge: JudgeFn | None = None,
    broker_config: SimBrokerConfig | None = None,
    cycle_config: CycleConfig | None = None,
) -> BacktestResult:
    watchlists = _load_watchlists(engine, start, end)
    if not watchlists:
        raise BacktestError(
            f"no watchlist snapshots between {start} and {end} — "
            "run `sontrader build-universe` first"
        )
    symbols = sorted(
        {symbol for symbols_on_day in watchlists.values() for symbol in symbols_on_day}
    )
    bars = _load_bars(engine, symbols, start, end)
    events = _load_events(engine, symbols, start, end)
    return replay(
        watchlists=watchlists,
        bars=bars,
        events=events,
        initial_cash=initial_cash,
        judge=judge,
        broker_config=broker_config,
        cycle_config=cycle_config,
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
) -> BacktestResult:
    dates = sorted(watchlists)
    if not dates:
        raise BacktestError("no cycle dates to replay")

    # 백테스트는 사람이 없다 — 결정적이어야 하므로 승인 큐를 거치면 안 된다
    # (engine/loop.py 참고). 호출자가 별도 cycle_config를 넘겨도 이 값은
    # 유지한다: 백테스트에서 승인 큐를 켜면 Deps.engine이 없어 즉시 에러이거나,
    # 있어도 사람 없이는 영원히 승인되지 않아 매매가 멈춘다.
    cycle_config = cycle_config or CycleConfig()
    if cycle_config.check_killswitch:
        cycle_config = replace(cycle_config, check_killswitch=False)

    judge = judge or (lambda _event: None)
    bar_view = InMemoryBarView(bars)
    broker = SimBroker(bars, initial_cash=initial_cash, config=broker_config)
    deps = Deps(broker=broker)

    held: dict[str, _HeldMeta] = {}
    used_event_ids: set[str] = set()
    last_exit_at: dict[str, datetime] = {}

    equity_curve: list[tuple[date, int]] = []
    fills: list[Fill] = []
    rejections: list[tuple[date, Rejection]] = []
    closed_trades: list[ClosedTrade] = []

    for day in dates:
        now = datetime.combine(day, time.min)
        view = bar_view.at(now)
        broker_positions = {p.symbol: p for p in broker.positions()}

        cash = broker.cash()
        mark_to_market = sum(
            bp.qty * (bar.close if (bar := view.latest(symbol)) is not None else bp.avg_price)
            for symbol, bp in broker_positions.items()
        )
        equity = int(cash + broker.pending_settlement + mark_to_market)

        day_events = tuple(events.get(day, ()))
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
        )

        result = run_cycle(ctx, deps, cycle_config)
        _apply_fills(result, held, used_event_ids, last_exit_at, fills, closed_trades)

        rejections.extend((day, r) for r in result.rejections)
        equity_curve.append((day, equity))

    final_positions = _reconstruct_positions({p.symbol: p for p in broker.positions()}, held)
    return BacktestResult(
        equity_curve=tuple(equity_curve),
        fills=tuple(fills),
        rejections=tuple(rejections),
        closed_trades=tuple(closed_trades),
        total_costs=broker.total_costs,
        final_cash=broker.cash(),
        final_positions=final_positions,
    )


def _reconstruct_positions(
    broker_positions: Mapping[str, object], held: Mapping[str, _HeldMeta]
) -> tuple[Position, ...]:
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


def _apply_fills(result, held, used_event_ids, last_exit_at, fills_out, closed_trades_out) -> None:
    """체결을 메모리 부기에 반영한다.

    포지션 변경 판정 자체는 `engine/fills.py`가 한다 — 실전(`apps/live.py`)이
    같은 규칙을 DB에 반영하므로, 규칙을 여기 복제하면 두 경로가 갈라진다.
    여기서는 백테스트에만 필요한 것(체결 로그, 종료된 거래 기록)을 더한다.
    """
    for order_result in result.order_results:
        if order_result.status in (OrderStatus.FILLED, OrderStatus.PARTIAL):
            fills_out.extend(order_result.fills)
            if order_result.order.side is Side.BUY and order_result.order.event_id is not None:
                used_event_ids.add(order_result.order.event_id)

    for change in fills.position_changes(result.order_results, held=frozenset(held)):
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
            )
        )


# --- DB 로딩 ---------------------------------------------------------------


def _load_watchlists(engine: Engine, start: date, end: date) -> dict[date, list[str]]:
    columns = db.watchlist_snapshots.c
    result: dict[date, list[str]] = {}
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(columns.date, columns.symbol)
            .where(columns.date >= start, columns.date <= end)
            .order_by(columns.date, columns.rank)
        )
        for day, symbol in rows:
            result.setdefault(day, []).append(symbol)
    return result


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

"""실전 Context 조립 (구현 계획 번호 없음 — `apps/live.py` 준비).

`apps/backtest.py`의 private 로더들(`_load_watchlists`/`_load_bars`/
`_load_events`)과 같은 역할이지만, 날짜 범위를 한꺼번에 읽는 백테스트와
달리 "지금" 시점 하나만 조립한다. DB를 아는 코드이므로 core가 아니라
engine에 둔다(구조 원칙 1).

## 왜 브로커를 모르는가

`positions`와 `cash`는 인자로 받는다 — 브로커(KIS 잔고조회) 호출과 그
결과의 재구성/불일치 판정은 `engine/reconcile.py`의 몫이다. 이 모듈이
`KisBroker`를 직접 알면 브로커별로 다른 테스트 이중화(웹소켓·HTTP 목)가
여기까지 번진다 — DB만 아는 채로 두면 순수 DB 픽스처만으로 검증할 수
있다.

## 이벤트를 "마지막으로 확인한 시각 이후"가 아니라 매번 lookback 창으로 읽는 이유

증분 상태(마지막 확인 시각)를 프로세스 메모리에만 두면 재시작 시
유실된다. `event_lookback`(기본 24시간) 창으로 매 사이클 다시 읽어도
안전한 이유는 `core/gate.py`가 이미 진입한 이벤트(`used_event_ids`)를
막기 때문이다. 진입 주문은 그 사이클에 바로 제출돼 `orders.event_id`에
남으므로, 같은 이벤트가 다음 사이클에 다시 읽혀도 이미 차단 대상이다.
재조회 비용은 이 규모(워치리스트 ≤50종목)에서 무시할 만하다.

## `used_event_ids`를 별도 테이블 없이 `orders`에서 구하는 이유

청산 주문도 진입 이벤트의 event_id를 그대로 실어 나른다(`core/diff.py`의
`_order()` — 전량 청산 시 `pos.event_id`를 넣는다). 그래서 `orders`
테이블에서 event_id가 있는 행을 전부 모으면 "이미 쓴 이벤트"가 보유
중이든 청산됐든 상관없이 다 나온다 — 별도 이력 테이블이 필요 없다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, time, timedelta

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.core.types import Bar, Context, Event, Judgment, Position
from sontrader.data import db
from sontrader.data import orders as orders_repo
from sontrader.engine.context import InMemoryBarView
from sontrader.logging_setup import traced

_SYMBOL_CHUNK = 500  # IN 절 바인드 변수 한도 대비 — apps/backtest.py와 동일한 규약
_DEFAULT_EVENT_LOOKBACK = timedelta(hours=24)
_DEFAULT_BAR_HISTORY = 300  # core/exit_rules.py 기본 ATR 창(14)+최대보유일 여유

JudgeFn = Callable[[Event], Judgment | None]


@traced
def build_context(
    engine: Engine,
    *,
    now: datetime,
    positions: tuple[Position, ...],
    cash: int,
    watchlist: tuple[str, ...],
    judge: JudgeFn,
    event_lookback: timedelta = _DEFAULT_EVENT_LOOKBACK,
    bar_history: int = _DEFAULT_BAR_HISTORY,
) -> Context:
    symbols = sorted(set(watchlist) | {p.symbol for p in positions})
    bars_by_symbol = _load_recent_daily_bars(engine, symbols, now, bar_history)
    view = InMemoryBarView(bars_by_symbol, now=now)

    mark_to_market = 0.0
    for pos in positions:
        bar = view.latest(pos.symbol)
        price = bar.close if bar is not None else pos.avg_price
        mark_to_market += pos.qty * price
    equity = int(cash + mark_to_market)

    events = _load_recent_events(engine, now, event_lookback)
    judgments = {event.event_id: v for event in events if (v := judge(event)) is not None}
    used_event_ids = _load_used_event_ids(engine)
    last_exit_at = _load_last_exit_at(engine)

    return Context(
        now=now,
        bars=view,
        watchlist=watchlist,
        positions=positions,
        new_events=tuple(events),
        judgments=judgments,
        cash=cash,
        equity=equity,
        used_event_ids=used_event_ids,
        last_exit_at=last_exit_at,
        pending_order_symbols=_load_pending_order_symbols(engine),
    )


def _load_pending_order_symbols(engine: Engine) -> frozenset[str]:
    """주문을 냈지만 체결이 아직 확인되지 않은 종목.

    `positions`와 합쳐야 "현재 상태"가 완성된다. 브로커 잔고에는 체결된 것만
    잡히므로, 이게 없으면 주문 직후 사이클이 "아직 아무것도 없다"고 판단해
    같은 종목을 다시 산다 (`core.types.Context.pending_order_symbols` 참고).

    호출 순서가 중요하다. `apps/live.py`는 `reconcile()`을 먼저 돌려 체결된
    주문을 FILLED로 확정하므로, 여기 남는 것은 **정말로 아직 미체결인 주문**
    뿐이다. 순서가 뒤집히면 이미 체결된 종목까지 막아 진입이 한 사이클 밀린다.

    PARTIAL도 포함한다. 일부만 체결된 매수의 잔량은 여전히 시장에 나가 있어,
    부족분을 다시 주문하면 결국 목표보다 많이 사게 된다.
    """
    return frozenset(record.symbol for record in orders_repo.list_unresolved(engine))


def _load_recent_daily_bars(
    engine: Engine, symbols: Sequence[str], now: datetime, count: int
) -> dict[str, list[Bar]]:
    if not symbols:
        return {}
    # 거래일 캘린더를 모르므로(구조 원칙 1) count 거래일을 안전하게 덮도록
    # 주말·공휴일을 감안해 넉넉히 뒤로 잡는다.
    start = now.date() - timedelta(days=count * 2 + 10)
    columns = db.stock_candles_1d.c
    result: dict[str, list[Bar]] = {symbol: [] for symbol in symbols}
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
                .where(columns.symbol.in_(chunk), columns.date >= start, columns.date <= now.date())
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
    return {symbol: bars[-count:] for symbol, bars in result.items()}


def _load_recent_events(engine: Engine, now: datetime, lookback: timedelta) -> list[Event]:
    columns = db.events.c
    start = now - lookback
    with engine.connect() as conn:
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
            .where(columns.ingested_at > start, columns.ingested_at <= now)
            .order_by(columns.ingested_at)
        )
        return [
            Event(
                event_id=row.event_id,
                symbol=row.symbol,
                corp_code=row.corp_code,
                event_type=row.event_type,
                norm_key=row.norm_key,
                title=row.title,
                published_at=row.published_at,
                ingested_at=row.ingested_at,
            )
            for row in rows
        ]


def _load_used_event_ids(engine: Engine) -> frozenset[str]:
    columns = db.orders.c
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(columns.event_id).where(columns.event_id.is_not(None)).distinct()
        )
        return frozenset(row.event_id for row in rows)


def _load_last_exit_at(engine: Engine) -> Mapping[str, datetime]:
    """종목별 마지막 매도 주문 시각 — 게이트 쿨다운 판정 근거."""
    columns = db.orders.c
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(columns.symbol, sa.func.max(columns.created_at))
            .where(columns.side == "sell")
            .group_by(columns.symbol)
        )
        return {symbol: ts for symbol, ts in rows}

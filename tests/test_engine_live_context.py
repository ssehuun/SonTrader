"""engine/live_context.py 테스트."""

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from sontrader.core.diff import to_orders
from sontrader.core.types import (
    ExitRule,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Target,
    TargetItem,
    Urgency,
)
from sontrader.data import db
from sontrader.data import orders as orders_repo
from sontrader.engine.live_context import build_context

NOW = datetime(2026, 3, 10, 15, 30)


def seed_bar(db_engine, *, symbol: str, day: date, close: int = 71000) -> None:
    with db_engine.begin() as conn:
        conn.execute(
            db.stock_candles_1d.insert().values(
                symbol=symbol, date=day, open=close, high=close, low=close, close=close, volume=100
            )
        )


def seed_event(db_engine, *, event_id: str, symbol: str = "005930", ingested_at=NOW) -> None:
    with db_engine.begin() as conn:
        conn.execute(
            db.events.insert().values(
                event_id=event_id,
                symbol=symbol,
                corp_code="00000001",
                event_type="earnings",
                norm_key=f"key:{event_id}",
                title="공시",
                published_at=ingested_at,
                ingested_at=ingested_at,
            )
        )


def seed_order(
    db_engine,
    *,
    order_id: str,
    symbol: str,
    side: Side,
    event_id: str | None,
    created_at: datetime,
    status: OrderStatus = OrderStatus.FILLED,
) -> None:
    order = Order(
        idempotency_key=f"{symbol}:{side.value}:{created_at.isoformat()}:{order_id}",
        symbol=symbol,
        side=side,
        qty=10,
        order_type=OrderType.MARKET,
        urgency=Urgency.NEXT_OPEN,
        ts=created_at,
        event_id=event_id,
    )
    orders_repo.insert(db_engine, order, order_id=order_id, status=status, created_at=created_at)


def make_position(symbol: str = "005930", *, qty=10, avg_price=70000.0, event_id=None) -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_price=avg_price,
        entered_at=NOW - timedelta(days=1),
        exit_rule=ExitRule(),
        event_id=event_id,
    )


def test_build_context_loads_bars_for_watchlist_and_held_symbols(db_engine):
    db.migrate(db_engine)
    seed_bar(db_engine, symbol="005930", day=date(2026, 3, 9), close=71000)
    seed_bar(db_engine, symbol="000660", day=date(2026, 3, 9), close=100000)

    ctx = build_context(
        db_engine,
        now=NOW,
        positions=(make_position("000660"),),
        cash=0,
        watchlist=("005930",),
        judge=lambda event: None,
    )

    assert ctx.bars.latest("005930") is not None
    assert ctx.bars.latest("000660") is not None


def test_build_context_limits_bars_to_history_count(db_engine):
    db.migrate(db_engine)
    for i in range(5):
        seed_bar(db_engine, symbol="005930", day=date(2026, 3, 1) + timedelta(days=i))

    ctx = build_context(
        db_engine,
        now=NOW,
        positions=(),
        cash=0,
        watchlist=("005930",),
        judge=lambda event: None,
        bar_history=3,
    )

    assert len(ctx.bars.history("005930", 10)) == 3


def test_build_context_computes_equity_from_cash_and_mark_to_market(db_engine):
    db.migrate(db_engine)
    seed_bar(db_engine, symbol="005930", day=date(2026, 3, 9), close=71000)

    ctx = build_context(
        db_engine,
        now=NOW,
        positions=(make_position("005930", qty=10),),
        cash=1_000_000,
        watchlist=(),
        judge=lambda event: None,
    )

    assert ctx.equity == 1_000_000 + 10 * 71000


def test_build_context_falls_back_to_avg_price_when_no_bar_available(db_engine):
    db.migrate(db_engine)

    ctx = build_context(
        db_engine,
        now=NOW,
        positions=(make_position("005930", qty=10, avg_price=65000.0),),
        cash=0,
        watchlist=(),
        judge=lambda event: None,
    )

    assert ctx.equity == int(10 * 65000.0)


def test_build_context_only_loads_events_within_lookback_window(db_engine):
    db.migrate(db_engine)
    seed_event(db_engine, event_id="recent", ingested_at=NOW - timedelta(hours=1))
    seed_event(db_engine, event_id="stale", ingested_at=NOW - timedelta(hours=48))

    ctx = build_context(
        db_engine,
        now=NOW,
        positions=(),
        cash=0,
        watchlist=(),
        judge=lambda event: None,
        event_lookback=timedelta(hours=24),
    )

    assert {e.event_id for e in ctx.new_events} == {"recent"}


def test_build_context_judgments_come_from_the_judge_callback(db_engine):
    from sontrader.core.types import Judgment

    db.migrate(db_engine)
    seed_event(db_engine, event_id="evt-1")
    seed_event(db_engine, event_id="evt-2")

    def judge(event):
        if event.event_id == "evt-1":
            return Judgment(
                event_id="evt-1",
                prompt_version="v1",
                model="test",
                verdict=True,
                confidence=0.9,
                exit_rule=ExitRule(),
            )
        return None

    ctx = build_context(
        db_engine,
        now=NOW,
        positions=(),
        cash=0,
        watchlist=(),
        judge=judge,
        event_lookback=timedelta(hours=24),
    )

    assert set(ctx.judgments) == {"evt-1"}
    assert {e.event_id for e in ctx.new_events} == {"evt-1", "evt-2"}


def test_build_context_used_event_ids_includes_entry_and_exit_orders(db_engine):
    db.migrate(db_engine)
    seed_event(db_engine, event_id="evt-1")
    seed_order(
        db_engine,
        order_id="ord-buy",
        symbol="005930",
        side=Side.BUY,
        event_id="evt-1",
        created_at=NOW - timedelta(days=2),
    )
    seed_order(
        db_engine,
        order_id="ord-sell",
        symbol="005930",
        side=Side.SELL,
        event_id="evt-1",
        created_at=NOW - timedelta(days=1),
    )

    ctx = build_context(
        db_engine, now=NOW, positions=(), cash=0, watchlist=(), judge=lambda event: None
    )

    assert ctx.used_event_ids == frozenset({"evt-1"})


def test_build_context_last_exit_at_uses_the_most_recent_sell_order(db_engine):
    db.migrate(db_engine)
    seed_order(
        db_engine,
        order_id="ord-sell-1",
        symbol="005930",
        side=Side.SELL,
        event_id=None,
        created_at=NOW - timedelta(days=10),
    )
    seed_order(
        db_engine,
        order_id="ord-sell-2",
        symbol="005930",
        side=Side.SELL,
        event_id=None,
        created_at=NOW - timedelta(days=1),
    )

    ctx = build_context(
        db_engine, now=NOW, positions=(), cash=0, watchlist=(), judge=lambda event: None
    )

    assert ctx.last_exit_at["005930"] == NOW - timedelta(days=1)


def test_build_context_passes_watchlist_through(db_engine):
    db.migrate(db_engine)

    ctx = build_context(
        db_engine,
        now=NOW,
        positions=(),
        cash=0,
        watchlist=("005930", "000660"),
        judge=lambda event: None,
    )

    assert ctx.watchlist == ("005930", "000660")


# --- 미체결 주문 (T2) ----------------------------------------------------------


def test_pending_order_symbols_is_empty_when_everything_is_filled(db_engine):
    db.migrate(db_engine)
    seed_order(
        db_engine,
        order_id="o1",
        symbol="005930",
        side=Side.BUY,
        event_id=None,
        created_at=NOW,
        status=OrderStatus.FILLED,
    )

    ctx = build_context(
        db_engine, now=NOW, positions=(), cash=0, watchlist=(), judge=lambda _e: None
    )

    assert ctx.pending_order_symbols == frozenset()


@pytest.mark.parametrize(
    "status", [OrderStatus.SUBMITTED, OrderStatus.UNKNOWN, OrderStatus.PARTIAL]
)
def test_unresolved_orders_become_pending_symbols(db_engine, status):
    """접수됐지만 체결이 확인되지 않은 주문은 '현재 상태'의 일부다.

    2026-08-20 실전에서 08:35 매수 5건이 미체결인 동안 08:39에 같은 5종목을
    또 주문했다. 브로커 잔고에는 체결된 것만 잡혀 "아직 아무것도 없다"로
    보였기 때문이다. PARTIAL도 포함해야 잔량이 있는데 부족분을 또 사지 않는다.
    """
    db.migrate(db_engine)
    seed_order(
        db_engine,
        order_id="o1",
        symbol="005930",
        side=Side.BUY,
        event_id=None,
        created_at=NOW,
        status=status,
    )

    ctx = build_context(
        db_engine, now=NOW, positions=(), cash=0, watchlist=(), judge=lambda _e: None
    )

    assert ctx.pending_order_symbols == frozenset({"005930"})


def test_pending_buy_stops_the_next_cycle_from_ordering_again(db_engine):
    """T2 재현: 미체결 매수가 있으면 diff가 같은 종목을 다시 사지 않는다."""
    db.migrate(db_engine)
    seed_bar(db_engine, symbol="005930", day=date(2026, 3, 9), close=10_000)
    seed_order(
        db_engine,
        order_id="o1",
        symbol="005930",
        side=Side.BUY,
        event_id=None,
        created_at=NOW,
        status=OrderStatus.SUBMITTED,
    )
    ctx = build_context(
        db_engine,
        now=NOW,
        positions=(),
        cash=10_000_000,
        watchlist=("005930",),
        judge=lambda _e: None,
    )
    target = Target((TargetItem("005930", 0.2, Urgency.NEXT_OPEN, exit_rule=ExitRule()),))

    assert to_orders(target, ctx) == []

    # 대조군: 미체결이 없으면 정상적으로 주문이 나간다
    empty = replace(ctx, pending_order_symbols=frozenset())
    assert [o.symbol for o in to_orders(target, empty)] == ["005930"]


def test_pending_sell_stops_a_duplicate_liquidation(db_engine):
    """미체결 매도가 있는데 또 팔면 잔고 부족으로 거부된다."""
    db.migrate(db_engine)
    seed_bar(db_engine, symbol="005930", day=date(2026, 3, 9), close=10_000)
    seed_order(
        db_engine,
        order_id="o1",
        symbol="005930",
        side=Side.SELL,
        event_id=None,
        created_at=NOW,
        status=OrderStatus.SUBMITTED,
    )
    ctx = build_context(
        db_engine,
        now=NOW,
        positions=(make_position("005930"),),
        cash=0,
        watchlist=(),
        judge=lambda _e: None,
    )

    assert to_orders(Target(()), ctx) == []
    assert [
        o.symbol for o in to_orders(Target(()), replace(ctx, pending_order_symbols=frozenset()))
    ] == ["005930"]

"""data/live_bars.py 테스트 (분봉 수집기)."""

from datetime import datetime

from sontrader.core.types import Bar
from sontrader.data import db, live_bars

NOW = datetime(2026, 3, 10, 9, 30)


def bar(
    *, ts=NOW, symbol="005930", open=71000, high=71500, low=70900, close=71200, volume=100
) -> Bar:
    return Bar(symbol=symbol, ts=ts, open=open, high=high, low=low, close=close, volume=volume)


def test_store_then_load_recent_round_trips(db_engine):
    db.migrate(db_engine)
    live_bars.store(db_engine, bar())

    [loaded] = live_bars.load_recent(db_engine, "005930", count=10)

    assert loaded == bar()


def test_store_upserts_on_same_symbol_and_minute(db_engine):
    db.migrate(db_engine)
    live_bars.store(db_engine, bar(close=71200, volume=100))
    live_bars.store(db_engine, bar(close=71800, volume=150))  # 재연결 후 같은 분 재집계

    [loaded] = live_bars.load_recent(db_engine, "005930", count=10)

    assert loaded.close == 71800
    assert loaded.volume == 150


def test_load_recent_returns_ascending_order_limited_to_count(db_engine):
    db.migrate(db_engine)
    for minute in range(5):
        live_bars.store(db_engine, bar(ts=datetime(2026, 3, 10, 9, 30 + minute)))

    loaded = live_bars.load_recent(db_engine, "005930", count=3)

    assert [b.ts.minute for b in loaded] == [32, 33, 34]  # 가장 최근 3개, 오름차순


def test_load_recent_only_returns_the_requested_symbol(db_engine):
    db.migrate(db_engine)
    live_bars.store(db_engine, bar(symbol="005930"))
    live_bars.store(db_engine, bar(symbol="000660"))

    loaded = live_bars.load_recent(db_engine, "005930", count=10)

    assert {b.symbol for b in loaded} == {"005930"}


def test_load_recent_returns_empty_list_when_nothing_stored(db_engine):
    db.migrate(db_engine)

    assert live_bars.load_recent(db_engine, "005930", count=10) == []

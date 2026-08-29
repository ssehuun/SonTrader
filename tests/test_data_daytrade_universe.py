"""데이트레이딩 감시 대상 테스트 (R31).

두 계층을 따로 본다 — `data/universe.py` 테스트와 같은 관례다:

- `select()`: DB 없이 순수 선정 규칙
- `build_snapshot()`: SQLite 위에서 D-1 판정과 **재계산 금지**만 얇게
"""

from __future__ import annotations

from datetime import date

import pytest

from sontrader.data import db
from sontrader.data.daytrade_universe import (
    DEFAULT_TOP_N,
    RESERVED_FOR_POSITIONS,
    WS_SUBSCRIBE_LIMIT,
    DaytradeUniverseError,
    build_snapshot,
    load_snapshot,
    select,
)

D1 = date(2026, 8, 25)  # 근거가 될 D-1
DAY = date(2026, 8, 26)  # 감시 대상을 쓸 날

BIG = 5_000_000_000  # 유동성 하한(10억)을 넉넉히 넘는 거래대금


# --- 선정 규칙 ---------------------------------------------------------------


def test_ranks_by_volume_descending():
    rows = [("100", 10, BIG), ("200", 30, BIG), ("300", 20, BIG)]

    result = select(rows)

    assert [e.symbol for e in result] == ["200", "300", "100"]
    assert [e.rank for e in result] == [1, 2, 3]


def test_the_liquidity_floor_filters_before_the_cut_not_after():
    """순서가 중요하다. 자른 뒤 거르면 하한에 걸린 종목이 슬롯을 먹고 사라져
    `top_n`보다 적게 남는다 — 감시 자리가 이유 없이 비는 것이다."""
    rows = [
        ("저가대량", 1_000, 1),  # 거래량 1위지만 거래대금이 바닥
        ("정상1", 100, BIG),
        ("정상2", 90, BIG),
    ]

    result = select(rows, min_trade_value=1_000_000_000, top_n=2)

    assert [e.symbol for e in result] == ["정상1", "정상2"]


def test_a_penny_stock_with_huge_volume_does_not_take_a_slot():
    """거래량만 보면 저가 대량 거래가 순위를 채운다 — 진입 물량을 소화할 수
    없는 종목이 감시 슬롯을 먹으면 낭비다."""
    rows = [("동전주", 10_000_000, 999_999_999), ("정상", 1, BIG)]

    assert [e.symbol for e in select(rows)] == ["정상"]


def test_rows_without_a_trade_value_are_dropped():
    """하한을 통과했는지 알 수 없는데 통과시키면 그게 곧 하한을 없애는 것이다."""
    rows = [("모름", 10_000, None), ("정상", 1, BIG)]

    assert [e.symbol for e in select(rows)] == ["정상"]


def test_top_n_cuts_to_the_slot_budget():
    rows = [(f"{i:03d}", i, BIG) for i in range(1, 51)]

    assert len(select(rows, top_n=36)) == 36


def test_ties_are_broken_deterministically():
    """같은 입력에 같은 답이 나와야 스냅샷을 재현할 수 있다."""
    rows = [("300", 10, BIG), ("100", 10, BIG + 1), ("200", 10, BIG)]

    first = [e.symbol for e in select(rows)]
    second = [e.symbol for e in select(list(reversed(rows)))]

    # 거래량 동률 → 거래대금 큰 순 → 종목코드 순
    assert first == second == ["100", "200", "300"]


def test_the_slot_budget_is_derived_not_hardcoded():
    """36이라는 숫자만 남으면 왜 36인지 모른다. 웹소켓 한도에서 유도된다."""
    assert DEFAULT_TOP_N == WS_SUBSCRIBE_LIMIT - RESERVED_FOR_POSITIONS
    assert WS_SUBSCRIBE_LIMIT == 41  # 2026-08-27 실측 (03-운영.md T18)
    assert DEFAULT_TOP_N == 36


@pytest.mark.parametrize(
    ("kwargs", "match"), [({"top_n": 0}, "top_n"), ({"min_trade_value": -1}, "min_trade_value")]
)
def test_nonsense_parameters_are_rejected(kwargs, match):
    with pytest.raises(ValueError, match=match):
        select([("100", 1, BIG)], **kwargs)


# --- D-1 판정과 저장 ---------------------------------------------------------


def seed_daily(engine, day, rows):
    with engine.begin() as conn:
        conn.execute(
            db.stock_candles_1d.insert(),
            [
                {
                    "symbol": s,
                    "date": day,
                    "open": 1000,
                    "high": 1000,
                    "low": 1000,
                    "close": 1000,
                    "volume": v,
                    "trade_value": tv,
                }
                for s, v, tv in rows
            ],
        )


def test_build_uses_the_previous_trading_day_not_the_calendar_day(db_engine):
    """휴일·주말을 캘린더 없이 넘긴다 — `as_of` 미만의 가장 최근 일봉이다."""
    db.migrate(db_engine)
    seed_daily(db_engine, D1, [("100", 50, BIG), ("200", 10, BIG)])

    # 달력상 어제(08-27)가 아니라 데이터가 있는 08-25를 근거로 삼는다.
    snapshot = build_snapshot(db_engine, as_of=date(2026, 8, 28))

    assert snapshot.source_date == D1
    assert snapshot.symbols == ("100", "200")


def test_the_snapshot_is_not_recomputed_on_a_rerun(db_engine):
    """**재계산 금지** (R19). 일봉은 수정주가라 기업행위 시점에 과거가 소급
    변경되므로, 다시 계산하면 그날과 다른 답이 나온다."""
    db.migrate(db_engine)
    seed_daily(db_engine, D1, [("100", 50, BIG), ("200", 10, BIG)])
    first = build_snapshot(db_engine, as_of=DAY)

    # D-1 데이터가 사후에 바뀌어도(기업행위 소급 수정을 흉내낸다)…
    with db_engine.begin() as conn:
        conn.execute(
            db.stock_candles_1d.update()
            .where(db.stock_candles_1d.c.symbol == "200")
            .values(volume=9_999_999)
        )
    second = build_snapshot(db_engine, as_of=DAY)

    # …감시 대상은 그날 뽑은 그대로다.
    assert second.symbols == first.symbols == ("100", "200")


def test_the_snapshot_round_trips_through_the_database(db_engine):
    db.migrate(db_engine)
    seed_daily(db_engine, D1, [("100", 50, BIG), ("200", 10, BIG)])

    built = build_snapshot(db_engine, as_of=DAY)
    loaded = load_snapshot(db_engine, DAY)

    assert loaded == built
    assert loaded.entries[0].volume == 50
    assert loaded.source_date == D1


def test_load_returns_none_when_nothing_was_stored(db_engine):
    db.migrate(db_engine)

    assert load_snapshot(db_engine, DAY) is None


def test_store_false_leaves_no_trace(db_engine):
    """스냅샷을 남기지 않고 규칙만 시험해 보는 경로."""
    db.migrate(db_engine)
    seed_daily(db_engine, D1, [("100", 50, BIG)])

    build_snapshot(db_engine, as_of=DAY, store=False)

    assert load_snapshot(db_engine, DAY) is None


def test_no_prior_daily_bars_is_an_explicit_failure(db_engine):
    """조용히 빈 감시 대상을 돌려주면 "오늘은 후보가 없었다"와 구별되지 않는다."""
    db.migrate(db_engine)

    with pytest.raises(DaytradeUniverseError, match="일봉이 없다"):
        build_snapshot(db_engine, as_of=DAY)


def test_everything_below_the_liquidity_floor_is_an_explicit_failure(db_engine):
    db.migrate(db_engine)
    seed_daily(db_engine, D1, [("100", 50, 1), ("200", 10, 2)])

    with pytest.raises(DaytradeUniverseError, match="유동성 하한"):
        build_snapshot(db_engine, as_of=DAY)

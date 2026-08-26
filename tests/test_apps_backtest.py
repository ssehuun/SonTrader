"""백테스트 러너 테스트 (구현 계획 5단계 마지막 조각).

두 계층을 따로 본다:

- `replay()`: DB 없이, 손으로 만든 워치리스트·봉·이벤트로 "진입 → 청산까지
  전체 루프가 실제로 돈다"는 것(02문서 5단계 검증 항목의 핵심)과 자산 곡선이
  맞게 계산되는지 확인한다.
- `run_backtest()`: SQLite 위에서 DB 읽기 배선(쿼리)이 맞는지만 얇게 확인한다
  — 세부 시뮬레이션 로직은 위에서 이미 검증했으므로 여기서 반복하지 않는다.
"""

from datetime import date, datetime, time

import pytest

from sontrader.adapters.broker_sim import SimBrokerConfig
from sontrader.apps.backtest import BacktestError, replay, run_backtest
from sontrader.core.gate import RejectReason
from sontrader.core.types import Bar, Event, ExitRule, Judgment
from sontrader.data import db

SYMBOL = "100"
DAY0 = date(2026, 3, 2)
DAY1 = date(2026, 3, 3)
DAY2 = date(2026, 3, 4)
DAY3 = date(2026, 3, 5)
ZERO_COST = SimBrokerConfig(commission_rate=0.0, tax_rate=0.0, slippage_bps=0.0)


def make_bar(symbol: str, day: date, *, open: int, close: int) -> Bar:  # noqa: A002
    return Bar(
        symbol=symbol,
        ts=datetime.combine(day, time.min),
        open=open,
        high=max(open, close),
        low=min(open, close),
        close=close,
        volume=1_000,
    )


def make_event(event_id: str, symbol: str, day: date) -> Event:
    ts = datetime.combine(day, time.min)
    return Event(
        event_id=event_id,
        symbol=symbol,
        corp_code="00000001",
        event_type="earnings",
        norm_key=f"key:{event_id}",
        title="공시",
        published_at=ts,
        ingested_at=ts,
    )


def always_enter(rule: ExitRule | None = None):
    def judge(event: Event) -> Judgment | None:
        return Judgment(
            event_id=event.event_id,
            prompt_version="v1",
            model="test-model",
            verdict=True,
            confidence=0.9,
            exit_rule=rule or ExitRule(),
        )

    return judge


# --- 전체 루프 관통: 진입 → 보유 → 청산 ------------------------------------------


def test_full_loop_from_entry_through_stop_exit():
    # day0 종가 10,000 이후 이벤트 → day1 시가(10,500) 진입.
    # day2 종가 9,000으로 급락해 고정 손절(10,500×0.95=9,975) 이탈 → day3 시가(8,500) 청산.
    bars = {
        SYMBOL: [
            make_bar(SYMBOL, DAY0, open=9_800, close=10_000),
            make_bar(SYMBOL, DAY1, open=10_500, close=10_600),
            make_bar(SYMBOL, DAY2, open=9_500, close=9_000),
            make_bar(SYMBOL, DAY3, open=8_500, close=8_500),
        ]
    }
    watchlists = {day: [SYMBOL] for day in (DAY0, DAY1, DAY2, DAY3)}
    events = {DAY0: [make_event("E1", SYMBOL, DAY0)]}

    result = replay(
        watchlists=watchlists,
        bars=bars,
        events=events,
        initial_cash=10_000_000,
        judge=always_enter(),
        broker_config=ZERO_COST,
    )

    assert len(result.fills) == 2
    buy, sell = result.fills
    assert buy.price == 10_500 and buy.qty == 200
    assert sell.price == 8_500 and sell.qty == 200
    assert result.final_positions == ()
    assert result.final_cash == 10_000_000 - 10_500 * 200  # 매도 대금은 아직 정산 전

    assert len(result.closed_trades) == 1
    trade = result.closed_trades[0]
    assert trade.symbol == SYMBOL
    assert trade.entry_price == 10_500
    assert trade.exit_price == 8_500
    assert trade.qty == 200
    assert trade.entered_at == datetime.combine(DAY1, time.min)
    assert trade.exit_at == datetime.combine(DAY3, time.min)
    assert result.total_costs == 0  # ZERO_COST 설정


def test_equity_curve_tracks_cash_plus_mark_to_market_plus_pending_settlement():
    bars = {
        SYMBOL: [
            make_bar(SYMBOL, DAY0, open=9_800, close=10_000),
            make_bar(SYMBOL, DAY1, open=10_500, close=10_600),
            make_bar(SYMBOL, DAY2, open=9_500, close=9_000),
            make_bar(SYMBOL, DAY3, open=8_500, close=8_500),
        ]
    }
    watchlists = {day: [SYMBOL] for day in (DAY0, DAY1, DAY2, DAY3)}
    events = {DAY0: [make_event("E1", SYMBOL, DAY0)]}

    result = replay(
        watchlists=watchlists,
        bars=bars,
        events=events,
        initial_cash=10_000_000,
        judge=always_enter(),
        broker_config=ZERO_COST,
    )

    curve = dict(result.equity_curve)
    assert curve[DAY0] == 10_000_000  # 아직 미체결
    assert curve[DAY1] == 7_900_000 + 200 * 10_600  # 진입 완료, 종가로 평가
    assert curve[DAY2] == 7_900_000 + 200 * 9_000  # 급락, 청산은 아직 이 사이클 처리 중
    assert curve[DAY3] == 7_900_000 + 200 * 8_500  # 매도 대금은 정산 대기, 시가로 평가한 값과 같다


# --- 게이트 거부 --------------------------------------------------------------


def test_rejections_are_recorded_with_their_date():
    held_symbols = [f"00{i}" for i in range(5)]
    bars = {
        symbol: [make_bar(symbol, day, open=10_000, close=10_000) for day in (DAY0, DAY1)]
        for symbol in held_symbols
    }
    bars["999"] = [make_bar("999", DAY0, open=10_000, close=10_000)]
    watchlists = {DAY0: [*held_symbols, "999"], DAY1: held_symbols}

    # 백테스트는 flat 시작이라 "이미 보유 중"을 만들 수 없으므로, 대신 같은 날
    # 동시 진입 5건(먼저 슬롯을 채움) + 신규 1건으로 슬롯 경합을 만든다.
    all_events = {
        DAY0: [
            *(make_event(f"H{i}", s, DAY0) for i, s in enumerate(held_symbols)),
            make_event("E1", "999", DAY0),
        ]
    }

    result = replay(
        watchlists=watchlists,
        bars=bars,
        events=all_events,
        initial_cash=10_000_000,
        judge=always_enter(),
        broker_config=ZERO_COST,
    )

    assert len(result.rejections) == 1
    day, rejection = result.rejections[0]
    assert day == DAY0
    assert rejection.symbol == "999"
    assert rejection.reason is RejectReason.SLOT_FULL


# --- 이벤트 재사용 차단 ------------------------------------------------------------


def test_event_id_stays_blocked_after_the_position_closes():
    bars = {
        SYMBOL: [
            make_bar(SYMBOL, DAY0, open=9_800, close=10_000),
            make_bar(SYMBOL, DAY1, open=10_500, close=10_600),
            make_bar(SYMBOL, DAY2, open=9_500, close=9_000),
            make_bar(SYMBOL, DAY3, open=8_500, close=8_500),
        ]
    }
    watchlists = {day: [SYMBOL] for day in (DAY0, DAY1, DAY2, DAY3)}
    # 같은 event_id("E1")가 청산 이후에도 다시 나타난다 — 재적재나 정정 시나리오.
    events = {
        DAY0: [make_event("E1", SYMBOL, DAY0)],
        DAY3: [make_event("E1", SYMBOL, DAY3)],
    }

    result = replay(
        watchlists=watchlists,
        bars=bars,
        events=events,
        initial_cash=10_000_000,
        judge=always_enter(),
        broker_config=ZERO_COST,
    )

    assert len(result.fills) == 2  # 매수 1 + 매도 1. E1 재사용으로 인한 재진입 없음
    day3_rejections = [r for d, r in result.rejections if d == DAY3]
    assert len(day3_rejections) == 1
    assert day3_rejections[0].reason is RejectReason.DUPLICATE_EVENT


# --- 기본값·경계 ----------------------------------------------------------------


def test_without_a_judge_no_event_ever_becomes_an_entry():
    bars = {SYMBOL: [make_bar(SYMBOL, DAY0, open=10_000, close=10_000)]}
    watchlists = {DAY0: [SYMBOL]}
    events = {DAY0: [make_event("E1", SYMBOL, DAY0)]}

    result = replay(watchlists=watchlists, bars=bars, events=events, initial_cash=10_000_000)

    assert result.fills == ()
    assert result.final_positions == ()
    assert dict(result.equity_curve) == {DAY0: 10_000_000}


def test_empty_watchlists_raises():
    with pytest.raises(BacktestError):
        replay(watchlists={}, bars={}, events={}, initial_cash=10_000_000)


# --- DB 배선 (run_backtest) ------------------------------------------------------


def seed_watchlist(engine, day, symbols):
    with engine.begin() as conn:
        conn.execute(
            db.watchlist_snapshots.insert(),
            [
                {"date": day, "symbol": s, "score": 0.1, "rank": i + 1}
                for i, s in enumerate(symbols)
            ],
        )


def seed_candles(engine, symbol, rows):
    with engine.begin() as conn:
        conn.execute(
            db.stock_candles_1d.insert(),
            [
                {
                    "symbol": symbol,
                    "date": day,
                    "open": o,
                    "high": max(o, c),
                    "low": min(o, c),
                    "close": c,
                    "volume": 1_000,
                }
                for day, o, c in rows
            ],
        )


def seed_event(engine, event_id, symbol, day):
    ts = datetime.combine(day, time.min)
    with engine.begin() as conn:
        conn.execute(
            db.events.insert().values(
                event_id=event_id,
                symbol=symbol,
                corp_code="00000001",
                event_type="earnings",
                norm_key=f"key:{event_id}",
                title="공시",
                published_at=ts,
                ingested_at=ts,
                raw_json={},
            )
        )


def test_run_backtest_reads_watchlist_bars_and_events_from_the_db(db_engine):
    db.migrate(db_engine)
    seed_watchlist(db_engine, DAY0, [SYMBOL])
    seed_watchlist(db_engine, DAY1, [SYMBOL])
    seed_candles(db_engine, SYMBOL, [(DAY0, 9_800, 10_000), (DAY1, 10_500, 10_600)])
    seed_event(db_engine, "E1", SYMBOL, DAY0)

    result = run_backtest(
        db_engine,
        start=DAY0,
        end=DAY1,
        initial_cash=10_000_000,
        judge=always_enter(),
        broker_config=ZERO_COST,
    )

    assert len(result.fills) == 1  # 진입만, 아직 청산 신호는 없다
    assert result.fills[0].price == 10_500
    assert len(result.final_positions) == 1
    assert result.final_positions[0].symbol == SYMBOL


def test_run_backtest_raises_without_any_watchlist_snapshot(db_engine):
    db.migrate(db_engine)

    with pytest.raises(BacktestError):
        run_backtest(db_engine, start=DAY0, end=DAY1, initial_cash=10_000_000)


def test_run_backtest_ignores_events_for_symbols_outside_the_universe(db_engine):
    db.migrate(db_engine)
    seed_watchlist(db_engine, DAY0, [SYMBOL])
    seed_candles(db_engine, SYMBOL, [(DAY0, 10_000, 10_000)])
    seed_event(db_engine, "E-OTHER", "999999", DAY0)  # 워치리스트에 없던 종목

    result = run_backtest(
        db_engine, start=DAY0, end=DAY0, initial_cash=10_000_000, judge=always_enter()
    )

    assert result.fills == ()


# --- 매매수량단위 (T7) ------------------------------------------------------


def test_replay_floors_entry_quantity_to_the_trading_unit():
    """진입 수량이 배수로 내려간다. 배수가 아니면 실전에서 KIS가 거부한다."""
    bars = {
        SYMBOL: [
            make_bar(SYMBOL, DAY0, open=9_800, close=10_000),
            make_bar(SYMBOL, DAY1, open=10_500, close=10_600),
        ]
    }
    watchlists = {day: [SYMBOL] for day in (DAY0, DAY1)}
    events = {DAY0: [make_event("E1", SYMBOL, DAY0)]}

    result = replay(
        watchlists=watchlists,
        bars=bars,
        events=events,
        initial_cash=10_000_000,
        judge=always_enter(),
        broker_config=ZERO_COST,
        trading_units={SYMBOL: 30},
    )

    # 단위가 1이면 200주(= test_full_loop...와 동일). 30 단위 → 180주.
    assert result.fills[0].qty == 180


def test_run_backtest_reads_trading_units_from_symbol_master(db_engine):
    db.migrate(db_engine)
    seed_watchlist(db_engine, DAY0, [SYMBOL])
    seed_watchlist(db_engine, DAY1, [SYMBOL])
    seed_candles(db_engine, SYMBOL, [(DAY0, 9_800, 10_000), (DAY1, 10_500, 10_600)])
    seed_event(db_engine, "E1", SYMBOL, DAY0)
    with db_engine.begin() as conn:
        conn.execute(
            db.symbol_master.insert().values(
                symbol=SYMBOL,
                name="테스트",
                market="KOSPI",
                trading_unit=30,
                updated_at=datetime.combine(DAY0, time.min),
            )
        )

    result = run_backtest(
        db_engine,
        start=DAY0,
        end=DAY1,
        initial_cash=10_000_000,
        judge=always_enter(),
        broker_config=ZERO_COST,
    )

    assert result.fills[0].qty == 180


def test_an_orphaned_broker_position_raises_instead_of_being_skipped():
    """브로커에 있는데 진입 정보가 없는 종목은 **조용히 넘어가면 안 된다.**

    이 상태가 2026-08-26에 발견된 부기 버그의 최종 증상이다 — 전략에게
    보이지 않는 채로 실제 보유가 남아 청산 규칙이 영영 안 걸리고, 게이트의
    슬롯 계산에서도 빠져 동시보유 상한이 무너졌다.
    """
    from sontrader.adapters.broker import BrokerPosition
    from sontrader.apps.backtest import _reconstruct_positions

    with pytest.raises(ValueError, match="orphaned position"):
        _reconstruct_positions({SYMBOL: BrokerPosition(SYMBOL, 10, 10_000.0)}, held={})

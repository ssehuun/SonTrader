"""백테스트 러너 테스트 (구현 계획 5단계 마지막 조각).

두 계층을 따로 본다:

- `replay()`: DB 없이, 손으로 만든 워치리스트·봉·이벤트로 "진입 → 청산까지
  전체 루프가 실제로 돈다"는 것(02문서 5단계 검증 항목의 핵심)과 자산 곡선이
  맞게 계산되는지 확인한다.
- `run_backtest()`: SQLite 위에서 DB 읽기 배선(쿼리)이 맞는지만 얇게 확인한다
  — 세부 시뮬레이션 로직은 위에서 이미 검증했으므로 여기서 반복하지 않는다.
"""

from datetime import date, datetime, time, timedelta

import pytest

from sontrader.adapters.broker_sim import SimBrokerConfig
from sontrader.apps.backtest import (
    BacktestError,
    BarInterval,
    ClosedTrade,
    HaltReport,
    SessionShape,
    partition_trades,
    replay,
    run_backtest,
)
from sontrader.core.gate import RejectReason
from sontrader.core.strategy import EntryTrigger, StrategyConfig
from sontrader.core.types import Bar, Event, ExitReason, ExitRule, Judgment
from sontrader.data import db
from sontrader.engine.loop import CycleConfig

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
    skipped = result.rejections[0]
    day, rejection = skipped.ts.date(), skipped
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
    day3_rejections = [r for r in result.rejections if r.ts.date() == DAY3]
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


# --- 분봉 재생 (R2c) --------------------------------------------------------
#
# 이 절이 확인하는 핵심은 하나다: **`core/`를 고치지 않고 분 단위 재생이 되는가.**
# 아래 테스트는 전부 `replay()`/`run_backtest()`에 분봉을 넣을 뿐이고,
# `core/exit_rules.py`·`core/strategy.py`·`core/gate.py`는 그대로다.


def make_minute_bar(symbol: str, ts: datetime, *, open: int, close: int) -> Bar:  # noqa: A002
    return Bar(
        symbol=symbol,
        ts=ts,
        open=open,
        high=max(open, close),
        low=min(open, close),
        close=close,
        volume=1_000,
    )


def minute_session(
    symbol: str, day: date, closes: list[int], *, start_minute: int = 0
) -> list[Bar]:
    """09:00부터 1분 간격 봉. `closes[i]`가 i번째 봉의 시가이자 종가다.

    시가=종가로 두면 "다음 봉 시가에 체결"이 그 봉의 종가와 같아져 체결가를
    눈으로 따라갈 수 있다.
    """
    base = datetime.combine(day, time(9, 0))
    return [
        make_minute_bar(symbol, base + timedelta(minutes=start_minute + i), open=c, close=c)
        for i, c in enumerate(closes)
    ]


def minute_cycle_config():
    """워치리스트 순위 진입 + 짧은 ATR 창. 분봉에서 스톱이 실제로 걸리게 한다."""
    return CycleConfig(
        strategy=StrategyConfig(
            entry_trigger=EntryTrigger.WATCHLIST_RANK,
            exit_rule=ExitRule(atr_period=2, max_hold_days=30),
        ),
        check_killswitch=False,
    )


def test_minute_replay_enters_and_exits_inside_a_single_day():
    """데이트레이딩의 최소 요건 — 같은 날 진입하고 같은 날 청산된다.

    `SimBroker`가 `urgency`와 무관하게 "주문 시각 다음 봉의 시가"에 체결하므로,
    봉이 분봉이면 그 "다음 봉"이 다음 개장이 아니라 1분 뒤가 된다. 구조 변경
    없이 당일 왕복이 되는 것은 여기까지다 — **시각 기반 강제 청산(15:20 전량
    정리)은 아직 없다**(`docs/system/02-매매-정교화.md` T24).
    """
    # 09:00~09:04 완만한 상승 → 09:05에 급락해 고정 손절(-5%) 이탈.
    closes = [10_000, 10_010, 10_020, 10_030, 10_040, 9_000, 8_900, 8_800]
    bars = {SYMBOL: minute_session(SYMBOL, DAY0, closes)}
    cycle_times = [bar.ts for bar in bars[SYMBOL]]

    result = replay(
        watchlists={DAY0: [SYMBOL]},
        bars=bars,
        events={},
        initial_cash=10_000_000,
        broker_config=ZERO_COST,
        cycle_config=minute_cycle_config(),
        watchlist_ranks={DAY0: {SYMBOL: 1}},
        cycle_times=cycle_times,
    )

    assert result.cycles == len(cycle_times)
    assert result.closed_trades, "분 단위로 진입·청산이 한 번도 일어나지 않았다"
    trade = result.closed_trades[0]
    assert trade.entered_at.date() == trade.exit_at.date() == DAY0
    assert trade.exit_at > trade.entered_at
    # 자산 곡선은 분봉이어도 하루 한 점이다 (apps/report.py의 CAGR·MDD 전제).
    assert [day for day, _ in result.equity_curve] == [DAY0]


def test_minute_replay_does_not_change_the_daily_path():
    """`cycle_times`를 주지 않으면 예전 그대로 날짜별 00:00 한 번이다."""
    bars = {
        SYMBOL: [
            make_bar(SYMBOL, DAY0, open=9_800, close=10_000),
            make_bar(SYMBOL, DAY1, open=10_500, close=10_600),
        ]
    }
    result = replay(
        watchlists={DAY0: [SYMBOL], DAY1: [SYMBOL]},
        bars=bars,
        events={DAY0: [make_event("E1", SYMBOL, DAY0)]},
        initial_cash=10_000_000,
        judge=always_enter(),
        broker_config=ZERO_COST,
    )
    assert result.cycles == 2
    assert result.halted_days == ()
    assert [day for day, _ in result.equity_curve] == [DAY0, DAY1]
    assert result.fills, "일봉 경로가 예전처럼 진입하지 못했다"


# --- T23: 결손 구간(시장 정지) ------------------------------------------------


def session_with(day, *, first_minute=0, length=380, drop=(), tail=(11,)):
    """하루치 봉을 만든다. `drop`은 빼는 인덱스(장중 결손), `tail`은 연속거래
    끝에서 몇 분 뒤에 단일가 봉을 붙일지(누적 간격)."""
    base = datetime.combine(day, time(9, 0)) + timedelta(minutes=first_minute)
    drop = set(drop)
    stamps = [base + timedelta(minutes=i) for i in range(length) if i not in drop]
    last = stamps[-1]
    for step in tail:
        last = last + timedelta(minutes=step)
        stamps.append(last)
    return [make_minute_bar(SYMBOL, ts, open=10_000, close=10_000) for ts in stamps]


def test_a_halt_is_an_interior_gap_not_a_low_bar_count():
    """서킷브레이커는 연속거래 구간 **안쪽**의 30분 결손이다 (T23)."""
    from sontrader.apps.backtest import _halted_days

    normal = session_with(DAY0)
    halted = session_with(DAY1, drop=range(100, 130))  # 장중 30분 결손
    assert _halted_days({SYMBOL: normal + halted}) == (DAY1,)


def test_a_shifted_session_is_not_a_halt():
    """세션 경계를 박지 않는다 — 실측 251일 중 11일이 09:00~15:30이 아니다.

    봉 **개수**로 재던 예전 판정("381봉 미만이면 정지")은 이 날들을 전부
    틀렸다. 개수가 아니라 **연속성**으로 보므로 개장·폐장이 밀려도 안 깨진다.
    """
    from sontrader.apps.backtest import _halted_days

    cases = {
        # 2026-01-02 개장식 — 10:00 개장, 320봉
        "late_open": session_with(DAY0, first_minute=60, length=320),
        # 2025-11-13 수능 — 세션 전체가 1시간 밀림
        "shifted": session_with(DAY1, first_minute=60),
        # 2026-08-20 — 장전 봉이 하나 앞에 떨어져 있다
        "pre_auction": [
            make_minute_bar(SYMBOL, datetime.combine(DAY2, time(8, 31)), open=10_000, close=10_000)
        ]
        + session_with(DAY2),
        # 2025-10-02 등 8일 — 마감 단일가가 15:30에 더해 15:32에 한 번 더
        "late_close": session_with(DAY3, tail=(11, 2)),
    }
    for name, bars in cases.items():
        assert _halted_days({SYMBOL: bars}) == (), f"{name}을 정지일로 잘못 잡았다"


def test_the_closing_auction_gap_is_not_a_halt():
    """15:19 → 15:30 사이 11분은 마감 단일가 호가접수라 **매일** 비어 있다.

    이걸 결손으로 세면 251거래일이 전부 정지일이 된다.
    """
    from sontrader.apps.backtest import session_shapes

    shape = session_shapes({SYMBOL: session_with(DAY0)})[DAY0]
    assert not shape.halted
    assert shape.halt_gaps == ()
    assert len(shape.post_auction) == 1  # 단일가 봉은 연속거래 바깥으로 분리된다
    assert shape.close_ts == datetime.combine(DAY0, time(9, 0)) + timedelta(minutes=379)


def test_session_bounds_come_from_the_data_not_from_a_clock():
    """R12가 요구하는 세션 경계 — `open_ts`/`close_ts`가 그대로 답이다."""
    from sontrader.apps.backtest import session_shapes

    # 10:00 개장 + 15:32 지연 단일가.
    bars = session_with(DAY0, first_minute=60, length=320, tail=(11, 2))
    shape = session_shapes({SYMBOL: bars})[DAY0]
    assert shape.open_ts == datetime.combine(DAY0, time(10, 0))
    assert shape.close_ts == datetime.combine(DAY0, time(10, 0)) + timedelta(minutes=319)
    assert len(shape.post_auction) == 2
    assert not shape.halted


def test_a_symbol_specific_gap_is_not_a_market_halt():
    """한 종목만 봉이 없으면 그 종목 사정(VI)이지 시장 정지가 아니다.

    실측에서 실제로 갈렸다 — 2026-03-05는 005930만 379봉이었고 다른 종목은
    381봉이었다(종목 고유 VI). 반면 서킷브레이커 9일은 유니버스 전체가 같은
    분을 잃었다.
    """
    from sontrader.apps.backtest import _halted_days

    full = session_with(DAY0)
    gapped = [
        Bar(
            symbol="200",
            ts=bar.ts,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        for bar in session_with(DAY0, drop=range(100, 130))
    ]
    assert _halted_days({SYMBOL: full, "200": gapped}) == ()


def test_a_signal_during_a_halt_fills_at_the_reopen_bar_not_the_stop_price():
    """T23의 핵심 규약 — 정지 중 발동한 신호는 **재개 후 첫 봉**에서 체결된다.

    보간으로 봉을 지어내면 "스톱 가격에 체결"이 되어 백테스트만 유리해진다.
    봉을 만들지 않으므로 `SimBroker._next_bar()`가 찾는 "다음 봉"이 곧 재개
    봉이고, 그사이 더 빠진 가격에 팔린다.
    """
    # 09:00~09:04 보유 구간 → 09:05에 스톱 이탈. 09:06~09:34는 정지(봉 없음).
    # 09:35 재개가는 스톱가보다 한참 아래다.
    pre = minute_session(SYMBOL, DAY0, [10_000, 10_010, 10_020, 10_030, 10_040, 9_000])
    reopen = minute_session(SYMBOL, DAY0, [7_000, 7_010], start_minute=35)
    bars = {SYMBOL: pre + reopen}
    cycle_times = [bar.ts for bar in bars[SYMBOL]]

    result = replay(
        watchlists={DAY0: [SYMBOL]},
        bars=bars,
        events={},
        initial_cash=10_000_000,
        broker_config=ZERO_COST,
        cycle_config=minute_cycle_config(),
        watchlist_ranks={DAY0: {SYMBOL: 1}},
        cycle_times=cycle_times,
        halts=HaltReport(
            days=(DAY0,), by_market={"KOSPI": (DAY0,)}, symbol_days=frozenset({(SYMBOL, DAY0)})
        ),
    )

    trade = result.closed_trades[0]
    # 스톱은 9_500 부근(진입가 10,010 × 0.95)에서 걸렸지만 체결은 재개가다.
    assert trade.exit_price == 7_000
    assert trade.exit_at == datetime.combine(DAY0, time(9, 35))
    # 정지일에 걸친 거래는 분리해서 볼 수 있다.
    normal, affected = partition_trades(result)
    assert normal == ()
    assert affected == result.closed_trades


# --- 분봉 모드의 이벤트 노출 -------------------------------------------------


def test_minute_mode_hides_an_event_until_its_ingested_at():
    """분봉에서는 `ingested_at` 이후 첫 사이클에만 노출한다.

    일봉은 사이클이 하루 한 번뿐이라 "그날 것 전부를 00:00에" 볼 수밖에
    없지만, 분 단위로 돌면 그 근사가 그대로 look-ahead가 된다 — 15:00 공시로
    09:01에 사게 된다.
    """
    from sontrader.apps.backtest import _events_by_cycle

    cycles = [datetime.combine(DAY0, time(9, 0)) + timedelta(minutes=i) for i in range(5)]
    event = Event(
        event_id="E1",
        symbol=SYMBOL,
        corp_code="00000001",
        event_type="earnings",
        norm_key="key:E1",
        title="공시",
        published_at=datetime.combine(DAY0, time(9, 2)),
        ingested_at=datetime.combine(DAY0, time(9, 2)),
    )

    gated = _events_by_cycle({DAY0: [event]}, cycles, gate=True)
    assert gated == {cycles[2]: (event,)}

    # 일봉 경로는 그날 첫 사이클에 전부 — 예전 그대로.
    legacy = _events_by_cycle({DAY0: [event]}, [datetime.combine(DAY0, time.min)], gate=False)
    assert legacy == {datetime.combine(DAY0, time.min): (event,)}


def test_an_event_ingested_after_the_close_rolls_to_the_next_session():
    """장 마감 뒤에 들어온 공시는 다음 거래일 첫 사이클에 보인다."""
    from sontrader.apps.backtest import _events_by_cycle

    cycles = [
        datetime.combine(DAY0, time(15, 30)),
        datetime.combine(DAY1, time(9, 0)),
    ]
    event = Event(
        event_id="E1",
        symbol=SYMBOL,
        corp_code="00000001",
        event_type="earnings",
        norm_key="key:E1",
        title="공시",
        published_at=datetime.combine(DAY0, time(17, 0)),
        ingested_at=datetime.combine(DAY0, time(17, 0)),
    )
    assert _events_by_cycle({DAY0: [event]}, cycles, gate=True) == {cycles[1]: (event,)}


# --- run_backtest 분봉 배선 --------------------------------------------------


def seed_minute_candles(engine, symbol, bars, *, source="rest"):
    with engine.begin() as conn:
        conn.execute(
            db.stock_candles_1m.insert(),
            [
                {
                    "symbol": symbol,
                    "ts": bar.ts,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "source": source,
                }
                for bar in bars
            ],
        )


def test_run_backtest_minute_mode_reads_1m_candles(db_engine):
    db.migrate(db_engine)
    closes = [10_000, 10_010, 10_020, 10_030, 10_040, 9_000, 8_900, 8_800]
    seed_minute_candles(db_engine, SYMBOL, minute_session(SYMBOL, DAY0, closes))

    result = run_backtest(
        db_engine,
        start=DAY0,
        end=DAY0,
        initial_cash=10_000_000,
        broker_config=ZERO_COST,
        cycle_config=minute_cycle_config(),
        interval=BarInterval.MINUTE,
        symbols=[SYMBOL],
    )

    assert result.cycles == len(closes)
    assert result.closed_trades
    # 봉이 8개뿐이지만 연속이라 정지일이 아니다. 개수로 재던 예전 판정은
    # 여기서 정지일이라고 답했다.
    assert result.halted_days == ()


@pytest.mark.parametrize("source", ["ws", None])
def test_run_backtest_minute_mode_reads_only_exchange_bars(db_engine, source):
    """`source='rest'`가 아닌 행은 읽지 않는다 (R8).

    웹소켓 집계 봉(`ws`)에는 시간외 거래가 섞여 있어(실측: 15:58 봉까지)
    그것으로 학습하면 실전에서 재현 불가능한 청산이 나온다.

    **`None`도 함께 본다.** 실측된 유령봉 13행이 `'ws'`가 아니라 NULL이었다 —
    `source` 컬럼이 생기기 전에 쓰인 레거시 행이다. 배제 목록(`!= 'ws'`)으로
    걸렀다면 SQL의 NULL 비교 때문에 그대로 통과했을 것이다.
    """
    db.migrate(db_engine)
    seed_minute_candles(
        db_engine, SYMBOL, minute_session(SYMBOL, DAY0, [10_000] * 5), source=source
    )

    with pytest.raises(BacktestError, match="no minute bars"):
        run_backtest(
            db_engine,
            start=DAY0,
            end=DAY0,
            initial_cash=10_000_000,
            interval=BarInterval.MINUTE,
            symbols=[SYMBOL],
        )


def test_pre_market_rows_do_not_reach_the_backtest(db_engine):
    """실측 재현 — 2026-08-20 08:31~08:37 장전 시간외 13행이 NULL 출처로
    들어와 있었다. 걸러지지 않으면 그날 세션이 08:31에 시작한 것처럼 보이고,
    개장 레인지(ORB)의 상단이 장전 가격으로 오염된다."""
    db.migrate(db_engine)
    session = minute_session(SYMBOL, DAY0, [10_000] * 5)
    ghost = [make_minute_bar(SYMBOL, datetime.combine(DAY0, time(8, 31)), open=9_000, close=9_000)]
    seed_minute_candles(db_engine, SYMBOL, session)
    seed_minute_candles(db_engine, SYMBOL, ghost, source=None)

    from sontrader.apps.backtest import _load_minute_bars

    loaded = _load_minute_bars(db_engine, [SYMBOL], DAY0, DAY0)
    assert [bar.ts for bar in loaded[SYMBOL]] == [bar.ts for bar in session]


def test_run_backtest_minute_mode_requires_an_explicit_universe(db_engine):
    db.migrate(db_engine)
    with pytest.raises(BacktestError, match="explicit universe"):
        run_backtest(
            db_engine,
            start=DAY0,
            end=DAY0,
            initial_cash=10_000_000,
            interval=BarInterval.MINUTE,
        )


def test_a_halt_in_one_market_is_not_erased_by_the_other():
    """**서킷브레이커는 시장별로 발동한다.**

    2026-07-28 10:13~10:43에 KOSPI가 멎는 동안 KOSDAQ 6종목은 29분 내내
    거래됐다(실측). 유니버스 전체를 한 덩어리로 합집합하면 그 구멍이 메워져
    정지가 없던 것처럼 보인다 — 실측 37종목에서 9일이 **2일**로 줄었다.
    """
    from sontrader.apps.backtest import halt_report

    kospi = session_with(DAY0, drop=range(100, 130))  # KOSPI만 30분 정지
    kosdaq = [
        Bar(
            symbol="200",
            ts=bar.ts,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        for bar in session_with(DAY0)  # KOSDAQ은 계속 거래
    ]
    bars = {SYMBOL: kospi, "200": kosdaq}
    markets = {SYMBOL: "KOSPI", "200": "KOSDAQ"}

    # 시장을 섞으면 정지가 사라진다 — 이게 고치기 전 동작이다.
    from sontrader.apps.backtest import _halted_days

    assert _halted_days(bars) == ()

    report = halt_report(bars, markets)
    assert report.days == (DAY0,)
    assert report.by_market == {"KOSPI": (DAY0,)}
    # 정지 표본은 **그 시장 종목만**이다.
    assert (SYMBOL, DAY0) in report.symbol_days
    assert ("200", DAY0) not in report.symbol_days


def test_a_symbol_missing_from_the_master_is_not_silently_dropped():
    """마스터에 없는 종목(상장폐지 등)은 `"?"` 그룹으로 남는다 — 버리면 그
    종목의 정지일이 조용히 사라진다."""
    from sontrader.apps.backtest import halt_report

    bars = {SYMBOL: session_with(DAY0, drop=range(100, 130))}
    report = halt_report(bars, markets={})
    assert report.by_market == {"?": (DAY0,)}
    assert (SYMBOL, DAY0) in report.symbol_days


def test_partition_trades_splits_by_symbol_not_by_day():
    """KOSPI가 멎은 날 거래된 KOSDAQ 종목은 정지 표본이 아니다."""
    from sontrader.apps.backtest import BacktestResult

    def trade(symbol, day):
        ts = datetime.combine(day, time(9, 30))
        return ClosedTrade(symbol, ts, ts + timedelta(minutes=10), 10_000.0, 10_100, 1)

    result = BacktestResult(
        equity_curve=(),
        fills=(),
        rejections=(),
        closed_trades=(trade("100", DAY0), trade("200", DAY0)),
        total_costs=0,
        final_cash=0,
        final_positions=(),
        halted_days=(DAY0,),
        halted_symbol_days=frozenset({("100", DAY0)}),
    )
    normal, affected = partition_trades(result)
    assert [t.symbol for t in normal] == ["200"]
    assert [t.symbol for t in affected] == ["100"]


# --- R12: 세션 종료 청산과 남은 봉 수 ------------------------------------------


def test_session_bars_remaining_counts_down_to_the_last_continuous_bar():
    """마감 단일가 봉은 세지 않는다 — R12의 "마지막 **연속거래** 봉"이다."""
    from sontrader.apps.backtest import _session_bars_remaining, session_shapes

    bars = session_with(DAY0, length=5)  # 09:00~09:04 + 단일가 1봉
    cycles = [bar.ts for bar in bars]
    remaining = _session_bars_remaining(cycles, session_shapes({SYMBOL: bars}))

    assert [remaining[ts] for ts in cycles[:5]] == [4, 3, 2, 1, 0]
    assert remaining[cycles[5]] == 0  # 단일가 봉 — 남은 연속거래 봉이 없다


def test_session_bars_remaining_follows_a_shifted_session():
    """10:00 개장이어도 세션 길이에서 유도하므로 그대로 맞는다."""
    from sontrader.apps.backtest import _session_bars_remaining, session_shapes

    bars = session_with(DAY0, first_minute=60, length=5)
    cycles = [bar.ts for bar in bars]
    remaining = _session_bars_remaining(cycles, session_shapes({SYMBOL: bars}))

    assert remaining[datetime.combine(DAY0, time(10, 0))] == 4
    assert remaining[datetime.combine(DAY0, time(10, 4))] == 0


def test_a_day_without_a_session_shape_leaves_the_count_unknown():
    """일봉 재생이 이 경로다 — None이라야 EOD가 발동하지 않는다."""
    from sontrader.apps.backtest import _session_bars_remaining

    cycles = [datetime.combine(DAY0, time.min)]
    assert _session_bars_remaining(cycles, {}) == {}


def daytrade_cycle_config(*, eod_exit_bars=None, max_hold_bars=None, cooldown_days=0):
    from sontrader.core.gate import GateConfig

    return CycleConfig(
        strategy=StrategyConfig(
            entry_trigger=EntryTrigger.WATCHLIST_RANK,
            exit_rule=ExitRule(
                atr_period=2, eod_exit_bars=eod_exit_bars, max_hold_bars=max_hold_bars
            ),
            exit_history_bars=400,
        ),
        gate=GateConfig(cooldown_days=cooldown_days),
        check_killswitch=False,
    )


def test_eod_exit_leaves_the_book_flat_at_the_end_of_the_day():
    """데이트레이딩의 정의 — 오버나이트 금지 (T24 = B, R12)."""
    # 완만한 상승이라 스톱은 안 걸린다. 세션 종료만이 청산 사유다.
    closes = [10_000 + i for i in range(12)]
    bars = {SYMBOL: minute_session(SYMBOL, DAY0, closes)}
    cycle_times = [bar.ts for bar in bars[SYMBOL]]

    result = replay(
        watchlists={DAY0: [SYMBOL]},
        bars=bars,
        events={},
        initial_cash=10_000_000,
        broker_config=ZERO_COST,
        cycle_config=daytrade_cycle_config(eod_exit_bars=3),
        watchlist_ranks={DAY0: {SYMBOL: 1}},
        cycle_times=cycle_times,
        shapes={
            DAY0: SessionShape(
                day=DAY0,
                open_ts=cycle_times[0],
                close_ts=cycle_times[-1],
                pre_auction=(),
                post_auction=(),
                halt_gaps=(),
                bars=len(cycle_times),
            )
        },
    )

    assert result.final_positions == (), "장 마감에 포지션이 남았다 — 오버나이트다"
    trade = result.closed_trades[0]
    assert trade.entered_at.date() == trade.exit_at.date() == DAY0
    # 남은 봉이 3개가 된 사이클에서 신호 → 다음 봉 체결. 마지막 봉보다 앞선다.
    assert trade.exit_at < cycle_times[-1]


def test_without_a_session_shape_the_same_config_holds_overnight():
    """`shapes`가 없으면 EOD는 발동하지 않는다 — 일봉 경로가 안 다치는 근거."""
    closes = [10_000 + i for i in range(12)]
    bars = {SYMBOL: minute_session(SYMBOL, DAY0, closes)}
    cycle_times = [bar.ts for bar in bars[SYMBOL]]

    result = replay(
        watchlists={DAY0: [SYMBOL]},
        bars=bars,
        events={},
        initial_cash=10_000_000,
        broker_config=ZERO_COST,
        cycle_config=daytrade_cycle_config(eod_exit_bars=3),
        watchlist_ranks={DAY0: {SYMBOL: 1}},
        cycle_times=cycle_times,
    )

    assert len(result.final_positions) == 1


def test_max_hold_bars_closes_a_position_inside_the_session():
    closes = [10_000 + i for i in range(12)]
    bars = {SYMBOL: minute_session(SYMBOL, DAY0, closes)}
    cycle_times = [bar.ts for bar in bars[SYMBOL]]

    result = replay(
        watchlists={DAY0: [SYMBOL]},
        bars=bars,
        events={},
        initial_cash=10_000_000,
        broker_config=ZERO_COST,
        cycle_config=daytrade_cycle_config(max_hold_bars=3, cooldown_days=1),
        watchlist_ranks={DAY0: {SYMBOL: 1}},
        cycle_times=cycle_times,
    )

    assert result.closed_trades
    trade = result.closed_trades[0]
    assert trade.entered_at.date() == trade.exit_at.date()
    # R14: 같은 날 재진입이 막히므로 거래는 1건뿐이다.
    assert len(result.closed_trades) == 1


# --- R16: 청산 사유가 체결 기록까지 살아서 도착하는가 --------------------------


def test_the_exit_reason_survives_the_whole_chain_to_the_trade_record():
    """전략이 판정한 사유가 `ClosedTrade`까지 온다.

    경로: `exit_rules.evaluate` → `TargetItem.exit_reason` → `Order.exit_reason`
    → `fills.Closed.exit_reason` → `ClosedTrade.exit_reason`. 중간에 한 군데만
    끊겨도 사후 추정으로 돌아가고, 추정은 스톱과 EOD가 같은 분에 겹치면 갈리지
    않는다.
    """
    closes = [10_000 + i for i in range(12)]
    bars = {SYMBOL: minute_session(SYMBOL, DAY0, closes)}
    cycle_times = [bar.ts for bar in bars[SYMBOL]]

    result = replay(
        watchlists={DAY0: [SYMBOL]},
        bars=bars,
        events={},
        initial_cash=10_000_000,
        broker_config=ZERO_COST,
        cycle_config=daytrade_cycle_config(eod_exit_bars=3),
        watchlist_ranks={DAY0: {SYMBOL: 1}},
        cycle_times=cycle_times,
        shapes={
            DAY0: SessionShape(
                day=DAY0,
                open_ts=cycle_times[0],
                close_ts=cycle_times[-1],
                pre_auction=(),
                post_auction=(),
                halt_gaps=(),
                bars=len(cycle_times),
            )
        },
    )

    assert result.closed_trades[0].exit_reason is ExitReason.EOD


def test_a_stop_exit_is_recorded_as_a_stop_not_as_a_session_end():
    closes = [10_000, 10_010, 10_020, 9_000, 8_900, 8_800]
    bars = {SYMBOL: minute_session(SYMBOL, DAY0, closes)}
    cycle_times = [bar.ts for bar in bars[SYMBOL]]

    result = replay(
        watchlists={DAY0: [SYMBOL]},
        bars=bars,
        events={},
        initial_cash=10_000_000,
        broker_config=ZERO_COST,
        cycle_config=daytrade_cycle_config(),
        watchlist_ranks={DAY0: {SYMBOL: 1}},
        cycle_times=cycle_times,
    )

    assert result.closed_trades[0].exit_reason is ExitReason.STOP


def test_exit_reason_breakdown_separates_the_buckets():
    from sontrader.apps.backtest import exit_reason_breakdown

    def trade(reason, entry, exit_price):
        ts = datetime.combine(DAY0, time(9, 30))
        return ClosedTrade(SYMBOL, ts, ts, float(entry), exit_price, 1, exit_reason=reason)

    trades = [
        trade(ExitReason.STOP, 10_000, 10_500),
        trade(ExitReason.STOP, 10_000, 9_500),
        trade(ExitReason.EOD, 10_000, 10_100),
        trade(None, 10_000, 9_900),  # 목표에서 빠진 리밸런싱 청산
    ]

    breakdown = exit_reason_breakdown(trades)

    assert breakdown["stop"][0] == 2
    assert breakdown["stop"][1] == 0.5  # 승률
    assert breakdown["eod"][0] == 1
    # 사유 없는 청산은 별도 버킷 — 청산 규칙이 아니라 리밸런싱이다.
    assert breakdown["rebalance"][0] == 1


# --- R23: 촉발했으나 못 산 후보 (G4 대조군 C0) --------------------------------


def test_a_skipped_candidate_records_the_cycle_time_not_just_the_day():
    """분봉은 하루에 사이클이 380번이다 — 날짜만 남기면 어느 시점의 거부인지
    알 수 없고, 이후 수익률의 시작점이 사라진다."""
    from sontrader.core.gate import GateConfig

    closes = [10_000, 10_010, 10_020, 10_030]
    bars = {
        "100": minute_session("100", DAY0, closes),
        "200": minute_session("200", DAY0, closes),
    }
    cycle_times = [bar.ts for bar in bars["100"]]

    result = replay(
        watchlists={DAY0: ["100", "200"]},
        bars=bars,
        events={},
        initial_cash=10_000_000,
        broker_config=ZERO_COST,
        cycle_config=CycleConfig(
            strategy=StrategyConfig(
                entry_trigger=EntryTrigger.WATCHLIST_RANK, exit_rule=ExitRule(atr_period=2)
            ),
            gate=GateConfig(max_positions=1),  # 슬롯 1개 → 하나는 반드시 밀린다
            check_killswitch=False,
        ),
        watchlist_ranks={DAY0: {"100": 1, "200": 2}},
        cycle_times=cycle_times,
    )

    assert result.rejections
    skipped = result.rejections[0]
    # 자정이 아니라 실제 사이클 시각이어야 한다.
    assert skipped.ts in cycle_times
    assert skipped.ts.time() != time.min
    assert skipped.reason is RejectReason.SLOT_FULL


def test_a_skipped_candidate_carries_the_price_at_that_moment():
    """이후 수익률의 기준점이다. 없으면 G4를 못 잰다."""
    from sontrader.core.gate import GateConfig

    bars = {
        "100": minute_session("100", DAY0, [10_000, 10_010, 10_020]),
        "200": minute_session("200", DAY0, [5_000, 5_010, 5_020]),
    }
    cycle_times = [bar.ts for bar in bars["100"]]

    result = replay(
        watchlists={DAY0: ["100", "200"]},
        bars=bars,
        events={},
        initial_cash=10_000_000,
        broker_config=ZERO_COST,
        cycle_config=CycleConfig(
            strategy=StrategyConfig(
                entry_trigger=EntryTrigger.WATCHLIST_RANK, exit_rule=ExitRule(atr_period=2)
            ),
            gate=GateConfig(max_positions=1),
            check_killswitch=False,
        ),
        watchlist_ranks={DAY0: {"100": 1, "200": 2}},
        cycle_times=cycle_times,
    )

    skipped = [r for r in result.rejections if r.symbol == "200"]
    assert skipped
    # 그 사이클의 마지막 완성 봉 종가여야 한다 (look-ahead 없이).
    for r in skipped:
        bar = next(b for b in bars["200"] if b.ts == r.ts)
        assert r.price == bar.close

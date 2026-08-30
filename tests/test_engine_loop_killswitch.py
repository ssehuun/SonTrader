"""run_cycle의 킬 스위치 연결 테스트 (구현 계획 6단계).

tests/test_engine_loop.py는 check_killswitch=False로 기본 배선만 본다.
여기서는 그 반대 — check_killswitch=True(기본값)일 때 킬 스위치가 신규
진입만 막고 청산은 통과시키는지, 막힌 후보가 사유와 함께 기록되는지를
확인한다.

승인 큐는 없다. 게이트를 통과한 신규 진입은 **그 사이클에 바로** 주문이
된다 — 사람이 건별로 개입하면 실전이 백테스트와 달라진다(01문서 §1.3).
"""

from datetime import datetime, timedelta

import pytest

from sontrader.adapters.broker import OrderResult
from sontrader.core.gate import RejectReason
from sontrader.core.types import (
    Bar,
    Context,
    Event,
    ExitRule,
    Fill,
    Judgment,
    OrderStatus,
    Position,
)
from sontrader.data import db
from sontrader.engine import killswitch
from sontrader.engine.loop import CycleConfig, Deps, run_cycle

NOW = datetime(2026, 3, 5, 9, 30)


class StubBars:
    def __init__(self, series=None):
        self._series = series or {}

    def history(self, symbol, count):
        return self._series.get(symbol, [])[-count:]

    def latest(self, symbol):
        bars = self._series.get(symbol, [])
        return bars[-1] if bars else None


class StubBroker:
    def __init__(self):
        self.calls = []

    def submit(self, orders, *, now):
        self.calls.append((orders, now))
        return [
            OrderResult(
                order=order,
                status=OrderStatus.FILLED,
                fills=(Fill(order_id=order.idempotency_key, price=10_000, qty=order.qty, ts=now),),
            )
            for order in orders
        ]

    def positions(self):
        return []

    def cash(self):
        return 0


def make_bars(symbol: str, closes: list[int], start: datetime = NOW) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            ts=start + timedelta(days=i),
            open=c,
            high=c,
            low=c,
            close=c,
            volume=1_000,
        )
        for i, c in enumerate(closes)
    ]


def make_event(event_id: str, symbol: str) -> Event:
    return Event(
        event_id=event_id,
        symbol=symbol,
        corp_code="00000001",
        event_type="earnings",
        norm_key=f"key:{event_id}",
        title="공시",
        published_at=NOW,
        ingested_at=NOW,
    )


def make_judgment(event_id: str, *, confidence: float = 0.8) -> Judgment:
    return Judgment(
        event_id=event_id,
        prompt_version="v1",
        model="test-model",
        verdict=True,
        confidence=confidence,
        exit_rule=ExitRule(),
    )


def make_ctx(**overrides) -> Context:
    base = dict(now=NOW, bars=StubBars(), watchlist=(), equity=10_000_000, cash=10_000_000)
    return Context(**{**base, **overrides})


def entry_ctx(event_id="E1", symbol="100") -> Context:
    event = make_event(event_id, symbol)
    return make_ctx(
        bars=StubBars({symbol: make_bars(symbol, [10_000])}),
        watchlist=(symbol,),
        new_events=(event,),
        judgments={event_id: make_judgment(event_id)},
    )


def test_check_killswitch_true_without_engine_raises():
    """fail-closed. 실전 배선에서 engine을 빠뜨리면 킬 스위치가 조용히
    무력화되는 대신 기동 시점에 죽어야 한다."""
    ctx = entry_ctx()
    broker = StubBroker()

    with pytest.raises(ValueError, match="Deps.engine"):
        run_cycle(ctx, Deps(broker=broker))


def test_new_entry_becomes_an_order_in_the_same_cycle(db_engine):
    db.migrate(db_engine)
    ctx = entry_ctx()
    broker = StubBroker()

    result = run_cycle(ctx, Deps(broker=broker, engine=db_engine))

    assert len(result.orders) == 1
    assert result.orders[0].symbol == "100"
    assert result.orders[0].side.value == "buy"
    assert result.rejections == ()


def test_kill_switch_blocks_new_entry(db_engine):
    db.migrate(db_engine)
    killswitch.engage(db_engine, now=NOW)
    ctx = entry_ctx()
    broker = StubBroker()

    result = run_cycle(ctx, Deps(broker=broker, engine=db_engine))

    assert result.orders == ()


def test_blocked_entry_is_recorded_with_its_reason(db_engine):
    """조용히 사라지면 안 된다 — 슬롯이 찼는지 킬 스위치였는지를
    구분할 수 있어야 "그날 왜 안 샀나"에 답한다(01문서 §6.6.1)."""
    db.migrate(db_engine)
    killswitch.engage(db_engine, now=NOW)
    ctx = entry_ctx()

    result = run_cycle(ctx, Deps(broker=StubBroker(), engine=db_engine))

    [rejection] = result.rejections
    assert rejection.symbol == "100"
    assert rejection.reason is RejectReason.KILL_SWITCH
    assert rejection.event_id == "E1"


def test_kill_switch_does_not_block_exits(db_engine):
    """청산까지 멈추면 리스크 관리가 아니라 리스크 방치가 된다."""
    db.migrate(db_engine)
    killswitch.engage(db_engine, now=NOW)
    position = Position(
        symbol="100", qty=200, avg_price=10_000.0, entered_at=NOW, exit_rule=ExitRule()
    )
    bars = make_bars("100", [10_000, 9_000])  # 고정 -5% 스톱 이탈 → 청산 신호
    ctx = make_ctx(bars=StubBars({"100": bars}), positions=(position,))

    result = run_cycle(ctx, Deps(broker=StubBroker(), engine=db_engine))

    assert len(result.orders) == 1
    assert result.orders[0].side.value == "sell"
    assert result.rejections == ()


def test_entries_resume_after_disengage(db_engine):
    db.migrate(db_engine)
    killswitch.engage(db_engine, now=NOW)
    ctx = entry_ctx()
    deps = Deps(broker=StubBroker(), engine=db_engine)

    assert run_cycle(ctx, deps).orders == ()

    killswitch.disengage(db_engine, now=NOW)

    assert len(run_cycle(ctx, deps).orders) == 1


def test_check_killswitch_false_needs_no_engine(db_engine):
    """백테스트 경로. 조작할 사람도 상태 저장소도 없다."""
    ctx = entry_ctx()
    config = CycleConfig(check_killswitch=False)

    result = run_cycle(ctx, Deps(broker=StubBroker()), config)

    assert len(result.orders) == 1
    assert result.orders[0].symbol == "100"

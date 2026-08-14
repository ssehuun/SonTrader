"""run_cycle의 승인 큐/킬 스위치 연결 테스트 (구현 계획 6단계).

tests/test_engine_loop.py는 require_approval=False(승인 큐 없음)로 기본
배선만 본다. 여기서는 그 반대 — require_approval=True(기본값)일 때
신규 진입이 바로 주문이 되지 않고 승인 큐를 거치는지, 킬 스위치가 신규
진입을 막는지를 확인한다.
"""

from datetime import datetime, timedelta

import pytest

from sontrader.adapters.broker import OrderResult
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
from sontrader.engine import approval, killswitch
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


class RecordingNotifier:
    def __init__(self):
        self.approval_requests = []
        self.messages = []

    def send_approval_request(self, proposal):
        self.approval_requests.append(proposal)

    def send_message(self, text):
        self.messages.append(text)


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
    base = dict(now=NOW, bars=StubBars(), watchlist=(), equity=10_000_000)
    return Context(**{**base, **overrides})


def entry_ctx(event_id="E1", symbol="100") -> Context:
    event = make_event(event_id, symbol)
    return make_ctx(
        bars=StubBars({symbol: make_bars(symbol, [10_000])}),
        watchlist=(symbol,),
        new_events=(event,),
        judgments={event_id: make_judgment(event_id)},
    )


def test_require_approval_true_without_engine_raises(db_engine):
    ctx = entry_ctx()
    broker = StubBroker()

    with pytest.raises(ValueError, match="Deps.engine"):
        run_cycle(ctx, Deps(broker=broker))


def test_new_entry_is_proposed_instead_of_submitted_immediately(db_engine):
    db.migrate(db_engine)
    ctx = entry_ctx()
    broker = StubBroker()

    result = run_cycle(ctx, Deps(broker=broker, engine=db_engine))

    assert result.orders == ()
    [proposal] = approval.list_pending(db_engine)
    assert proposal.symbol == "100"
    assert proposal.event_id == "E1"


def test_notifier_receives_the_approval_request(db_engine):
    db.migrate(db_engine)
    ctx = entry_ctx()
    broker = StubBroker()
    notifier = RecordingNotifier()

    run_cycle(ctx, Deps(broker=broker, engine=db_engine, notifier=notifier))

    assert len(notifier.approval_requests) == 1
    assert notifier.approval_requests[0].symbol == "100"


def test_approved_proposal_becomes_an_order_on_the_next_cycle(db_engine):
    db.migrate(db_engine)
    ctx = entry_ctx()
    broker = StubBroker()
    deps = Deps(broker=broker, engine=db_engine)

    run_cycle(ctx, deps)  # 1회차: 제안만 생성
    [proposal] = approval.list_pending(db_engine)
    approval.decide(db_engine, proposal.proposal_id, approve=True, now=NOW)

    result = run_cycle(ctx, deps)  # 2회차: 승인된 제안이 주문이 된다

    assert len(result.orders) == 1
    assert result.orders[0].symbol == "100"
    assert result.orders[0].side.value == "buy"


def test_rejected_proposal_never_becomes_an_order(db_engine):
    db.migrate(db_engine)
    ctx = entry_ctx()
    broker = StubBroker()
    deps = Deps(broker=broker, engine=db_engine)

    run_cycle(ctx, deps)
    [proposal] = approval.list_pending(db_engine)
    approval.decide(db_engine, proposal.proposal_id, approve=False, now=NOW)

    result = run_cycle(ctx, deps)

    assert result.orders == ()


def test_expired_proposal_never_becomes_an_order_and_notifies(db_engine):
    db.migrate(db_engine)
    ctx = entry_ctx()
    broker = StubBroker()
    notifier = RecordingNotifier()
    deps = Deps(broker=broker, engine=db_engine, notifier=notifier)
    config = CycleConfig(approval_ttl=timedelta(minutes=1))

    run_cycle(ctx, deps, config)
    later_ctx = make_ctx(
        now=NOW + timedelta(hours=1),
        bars=ctx.bars,
        watchlist=ctx.watchlist,
    )

    result = run_cycle(later_ctx, deps, config)

    assert result.orders == ()
    assert approval.list_pending(db_engine) == []
    assert any("만료" in msg for msg in notifier.messages)


def test_held_position_exit_bypasses_approval_queue(db_engine):
    db.migrate(db_engine)
    position = Position(
        symbol="100", qty=200, avg_price=10_000.0, entered_at=NOW, exit_rule=ExitRule()
    )
    bars = make_bars("100", [10_000, 9_000])  # 고정 -5% 스톱 이탈 → 청산 신호
    ctx = make_ctx(bars=StubBars({"100": bars}), positions=(position,))
    broker = StubBroker()

    result = run_cycle(ctx, Deps(broker=broker, engine=db_engine))

    assert len(result.orders) == 1
    assert result.orders[0].side.value == "sell"
    assert approval.list_pending(db_engine) == []  # 청산은 승인 큐를 타지 않는다


def test_kill_switch_blocks_new_entry_proposals(db_engine):
    db.migrate(db_engine)
    killswitch.engage(db_engine, now=NOW)
    ctx = entry_ctx()
    broker = StubBroker()

    result = run_cycle(ctx, Deps(broker=broker, engine=db_engine))

    assert result.orders == ()
    assert approval.list_pending(db_engine) == []


def test_kill_switch_blocks_pickup_of_already_approved_proposals(db_engine):
    db.migrate(db_engine)
    ctx = entry_ctx()
    broker = StubBroker()
    deps = Deps(broker=broker, engine=db_engine)

    run_cycle(ctx, deps)
    [proposal] = approval.list_pending(db_engine)
    approval.decide(db_engine, proposal.proposal_id, approve=True, now=NOW)
    killswitch.engage(db_engine, now=NOW)

    result = run_cycle(ctx, deps)

    assert result.orders == ()
    # 승인된 상태 그대로 큐에 남아 있다 — 소멸하지 않는다.
    [still_approved] = approval.pull_approved(db_engine)
    assert still_approved.proposal_id == proposal.proposal_id


def test_require_approval_false_skips_the_queue_entirely(db_engine):
    ctx = entry_ctx()
    broker = StubBroker()
    config = CycleConfig(require_approval=False)

    result = run_cycle(ctx, Deps(broker=broker), config)

    assert len(result.orders) == 1
    assert result.orders[0].symbol == "100"

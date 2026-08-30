"""run_cycle 테스트 (구현 계획 5단계).

strategy → gate → diff → broker.submit이 실제로 이어지는지, 그리고 게이트
거부(rejections)가 CycleResult까지 살아서 전달되는지를 확인한다. 각 단계
자체의 규칙(청산 경계값, 슬롯 상한 등)은 이미 core 쪽 테스트가 다루므로
여기서는 조립만 본다.

킬 스위치(6단계)는 `check_killswitch=False`로 꺼둔다 — 이 파일은 상태
저장소 없이 성립하는 기본 배선만 본다. 킬 스위치 자체의 동작은
tests/test_engine_loop_killswitch.py가 다룬다.
"""

from datetime import datetime, timedelta

from sontrader.adapters.broker import OrderResult
from sontrader.core.gate import GateConfig, RejectReason
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
from sontrader.engine.loop import CycleConfig, Deps, run_cycle

NOW = datetime(2026, 3, 5, 9, 30)
ENTRY_TS = datetime(2026, 3, 1, 0, 0)
NO_KILLSWITCH = CycleConfig(check_killswitch=False)


class StubBars:
    def __init__(self, series: dict[str, list[Bar]] | None = None) -> None:
        self._series = series or {}

    def history(self, symbol: str, count: int) -> list[Bar]:
        return self._series.get(symbol, [])[-count:]

    def latest(self, symbol: str) -> Bar | None:
        bars = self._series.get(symbol, [])
        return bars[-1] if bars else None


class StubBroker:
    """제출된 주문을 그대로 FILLED로 되돌려주는 브로커. 호출 인자를 기록해 둔다."""

    def __init__(self) -> None:
        self.calls: list[tuple[list, datetime]] = []

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


def make_bars(symbol: str, closes: list[int], start: datetime = ENTRY_TS) -> list[Bar]:
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


def make_position(symbol: str, *, qty: int = 200, avg_price: float = 10_000.0) -> Position:
    return Position(
        symbol=symbol, qty=qty, avg_price=avg_price, entered_at=ENTRY_TS, exit_rule=ExitRule()
    )


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


# --- 기본 조립 ------------------------------------------------------------------


def test_new_entry_reaches_the_broker_as_a_buy_order():
    event = make_event("E1", "100")
    ctx = make_ctx(
        bars=StubBars({"100": make_bars("100", [10_000])}),
        watchlist=("100",),
        new_events=(event,),
        judgments={"E1": make_judgment("E1")},
    )
    broker = StubBroker()

    result = run_cycle(ctx, Deps(broker=broker), NO_KILLSWITCH)

    assert len(result.orders) == 1
    assert result.orders[0].symbol == "100"
    assert len(result.order_results) == 1
    assert result.order_results[0].status is OrderStatus.FILLED
    assert result.rejections == ()


def test_broker_is_called_with_now_even_when_there_are_no_orders():
    ctx = make_ctx()
    broker = StubBroker()

    result = run_cycle(ctx, Deps(broker=broker), NO_KILLSWITCH)

    assert result.orders == ()
    assert broker.calls == [([], NOW)]


def test_exit_signal_reaches_the_broker_as_a_sell_order():
    position = make_position("100")
    bars = make_bars("100", [10_000, 9_000])  # 고정 -5% 스톱(9500) 이탈
    ctx = make_ctx(bars=StubBars({"100": bars}), positions=(position,))
    broker = StubBroker()

    result = run_cycle(ctx, Deps(broker=broker), NO_KILLSWITCH)

    assert len(result.orders) == 1
    assert result.orders[0].symbol == "100"
    assert result.orders[0].side.value == "sell"


def test_cycle_result_target_is_the_gated_target_not_the_raw_strategy_output():
    held = [make_position(f"00{i}") for i in range(5)]
    bars = {p.symbol: make_bars(p.symbol, [10_000, 10_000]) for p in held}
    event = make_event("E1", "999")
    ctx = make_ctx(
        bars=StubBars(bars),
        positions=tuple(held),
        watchlist=("999",),
        new_events=(event,),
        judgments={"E1": make_judgment("E1")},
    )
    broker = StubBroker()

    result = run_cycle(ctx, Deps(broker=broker), NO_KILLSWITCH)

    # 5슬롯이 이미 다 찼으므로 신규 진입은 최종 target에 없다.
    assert result.target.get("999") is None
    assert {item.symbol for item in result.target} == {p.symbol for p in held}


# --- 게이트 거부 전파 -------------------------------------------------------------


def test_rejections_from_the_gate_propagate_to_the_cycle_result():
    held = [make_position(f"00{i}") for i in range(5)]
    bars = {p.symbol: make_bars(p.symbol, [10_000, 10_000]) for p in held}
    event = make_event("E1", "999")
    ctx = make_ctx(
        bars=StubBars(bars),
        positions=tuple(held),
        watchlist=("999",),
        new_events=(event,),
        judgments={"E1": make_judgment("E1")},
    )
    broker = StubBroker()

    result = run_cycle(ctx, Deps(broker=broker), NO_KILLSWITCH)

    # held 5종목은 목표 비중과 현재 비중이 일치해 주문이 없고, 신규는 슬롯이
    # 없어 거부된다 — 그래서 이번 사이클엔 주문이 하나도 나가지 않는다.
    assert result.orders == ()
    assert len(result.rejections) == 1
    assert result.rejections[0].symbol == "999"
    assert result.rejections[0].reason is RejectReason.SLOT_FULL


def test_custom_gate_config_is_applied_through_cycle_config():
    events = (make_event("A", "100"), make_event("B", "200"))
    judgments = {
        "A": make_judgment("A", confidence=0.9),
        "B": make_judgment("B", confidence=0.5),
    }
    bars = StubBars({"100": make_bars("100", [10_000]), "200": make_bars("200", [10_000])})
    ctx = make_ctx(bars=bars, watchlist=("100", "200"), new_events=events, judgments=judgments)
    broker = StubBroker()
    config = CycleConfig(gate=GateConfig(max_positions=1), check_killswitch=False)

    result = run_cycle(ctx, Deps(broker=broker), config)

    # 확신도가 더 높은 100만 슬롯을 차지하고, 200은 거부된다.
    assert len(result.orders) == 1
    assert result.orders[0].symbol == "100"
    assert len(result.rejections) == 1
    assert result.rejections[0].symbol == "200"
    assert result.rejections[0].reason is RejectReason.SLOT_FULL


# --- order_results 정합성 --------------------------------------------------------


def test_order_results_correspond_one_to_one_with_orders():
    event = make_event("E1", "100")
    ctx = make_ctx(
        bars=StubBars({"100": make_bars("100", [10_000])}),
        watchlist=("100",),
        new_events=(event,),
        judgments={"E1": make_judgment("E1")},
    )
    broker = StubBroker()

    result = run_cycle(ctx, Deps(broker=broker), NO_KILLSWITCH)

    assert len(result.orders) == len(result.order_results)
    assert result.order_results[0].order == result.orders[0]

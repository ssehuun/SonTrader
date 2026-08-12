"""목표 → 주문 변환 테스트 (구현 계획 4단계).

두 가지에 무게를 둔다:

1. **청산이 어떤 이유로도 막히지 않는다** — 봉이 없어도, 금액이 작아도, band
   안에 있어도 나가야 한다.
2. **같은 입력이면 같은 주문** — 설계 2.4절이 중복 주문을 원리적으로 막는다고
   말하는 근거이자, 멱등 키가 성립하는 전제다.
"""

from datetime import datetime, timedelta

import pytest

from sontrader.core.diff import DiffConfig, to_orders
from sontrader.core.types import (
    Bar,
    Context,
    ExitRule,
    OrderType,
    Position,
    Side,
    Target,
    TargetItem,
    Urgency,
)

NOW = datetime(2026, 3, 10, 9, 30)
EQUITY = 10_000_000


class StubBars:
    """symbol → 종가. 없는 종목은 봉이 없는 상태를 흉내낸다."""

    def __init__(self, closes: dict[str, int] | None = None) -> None:
        self._closes = closes or {}

    def history(self, symbol: str, count: int) -> list[Bar]:
        bar = self.latest(symbol)
        return [bar] if bar else []

    def latest(self, symbol: str) -> Bar | None:
        close = self._closes.get(symbol)
        if close is None:
            return None
        return Bar(symbol, NOW, close, close, close, close, 1_000)


def make_ctx(*, closes: dict[str, int] | None = None, equity: int = EQUITY, **overrides) -> Context:
    base = dict(now=NOW, bars=StubBars(closes), watchlist=(), equity=equity)
    return Context(**{**base, **overrides})


def make_position(symbol: str, qty: int, *, event_id: str | None = None) -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_price=10_000.0,
        entered_at=NOW - timedelta(days=2),
        exit_rule=ExitRule(),
        event_id=event_id,
    )


def entry(symbol: str, weight: float = 0.2, *, event_id: str | None = None) -> TargetItem:
    return TargetItem(
        symbol=symbol,
        weight=weight,
        urgency=Urgency.NEXT_OPEN,
        exit_rule=ExitRule(),
        event_id=event_id,
    )


# --- 기본 변환 ----------------------------------------------------------------


def test_new_entry_sizes_from_equity_and_last_close():
    ctx = make_ctx(closes={"100": 10_000})

    orders = to_orders(Target((entry("100", 0.2),)), ctx)

    assert len(orders) == 1
    order = orders[0]
    assert order.symbol == "100"
    assert order.side is Side.BUY
    assert order.qty == 200  # 1,000만 × 20% / 10,000원
    assert order.order_type is OrderType.MARKET
    assert order.urgency is Urgency.NEXT_OPEN
    assert order.ts == NOW  # broker_sim이 "다음 봉"을 찾는 기준 (5단계)


def test_quantity_floors_to_whole_shares():
    ctx = make_ctx(closes={"100": 33_333})

    orders = to_orders(Target((entry("100", 0.2),)), ctx)

    assert orders[0].qty == 60  # 2,000,000 / 33,333 = 60.0006 → 60


def test_holding_at_target_produces_no_order():
    ctx = make_ctx(closes={"100": 10_000}, positions=(make_position("100", 200),))

    assert to_orders(Target((entry("100", 0.2),)), ctx) == []


def test_partial_sell_when_target_weight_is_below_current():
    ctx = make_ctx(closes={"100": 10_000}, positions=(make_position("100", 300),))

    orders = to_orders(Target((entry("100", 0.2),)), ctx)

    assert len(orders) == 1
    assert orders[0].side is Side.SELL
    assert orders[0].qty == 100


def test_entry_urgency_is_carried_onto_the_order():
    ctx = make_ctx(closes={"100": 10_000})
    item = TargetItem("100", 0.2, Urgency.IMMEDIATE, ExitRule())

    assert to_orders(Target((item,)), ctx)[0].urgency is Urgency.IMMEDIATE


# --- 청산은 무엇으로도 막지 않는다 ---------------------------------------------


def test_symbol_absent_from_target_is_fully_liquidated():
    ctx = make_ctx(closes={"100": 10_000}, positions=(make_position("100", 200),))

    orders = to_orders(Target(()), ctx)

    assert len(orders) == 1
    assert orders[0].side is Side.SELL
    assert orders[0].qty == 200
    assert orders[0].urgency is Urgency.IMMEDIATE


def test_zero_weight_item_liquidates_and_keeps_its_urgency():
    ctx = make_ctx(closes={"100": 10_000}, positions=(make_position("100", 200),))
    target = Target((TargetItem("100", 0.0, Urgency.IMMEDIATE),))

    orders = to_orders(target, ctx)

    assert orders[0].side is Side.SELL
    assert orders[0].qty == 200
    assert orders[0].urgency is Urgency.IMMEDIATE


def test_liquidation_happens_without_a_price_bar():
    # 시세 조회 실패가 청산을 막는 경로를 남기지 않는다.
    ctx = make_ctx(closes={}, positions=(make_position("100", 200),))

    orders = to_orders(Target(()), ctx)

    assert len(orders) == 1
    assert orders[0].qty == 200


def test_liquidation_ignores_the_minimum_order_value():
    ctx = make_ctx(closes={"100": 100}, positions=(make_position("100", 1),))

    orders = to_orders(Target(()), ctx, DiffConfig(min_order_value=1_000_000))

    assert len(orders) == 1
    assert orders[0].qty == 1


def test_liquidation_ignores_the_no_trade_band():
    ctx = make_ctx(closes={"100": 10_000}, positions=(make_position("100", 1),))

    orders = to_orders(Target(()), ctx, DiffConfig(no_trade_band=0.9))

    assert len(orders) == 1


def test_exits_come_before_other_orders():
    ctx = make_ctx(
        closes={"100": 10_000, "200": 10_000},
        positions=(make_position("100", 200),),
    )

    orders = to_orders(Target((entry("200", 0.2),)), ctx)

    assert [o.side for o in orders] == [Side.SELL, Side.BUY]
    assert orders[0].symbol == "100"


# --- no-trade band / 최소 주문금액 ---------------------------------------------


def test_small_rebalance_inside_the_band_is_skipped():
    # 목표 200주 vs 보유 195주 → 5주(50,000원 = 자산의 0.5%) < band 2%
    ctx = make_ctx(closes={"100": 10_000}, positions=(make_position("100", 195),))

    assert to_orders(Target((entry("100", 0.2),)), ctx) == []


def test_rebalance_outside_the_band_goes_through():
    # 목표 200주 vs 보유 150주 → 50주(500,000원 = 자산의 5%) > band 2%
    ctx = make_ctx(closes={"100": 10_000}, positions=(make_position("100", 150),))

    orders = to_orders(Target((entry("100", 0.2),)), ctx)

    assert len(orders) == 1
    assert orders[0].side is Side.BUY
    assert orders[0].qty == 50


def test_order_below_the_minimum_value_is_skipped():
    ctx = make_ctx(closes={"100": 10_000})

    orders = to_orders(Target((entry("100", 0.2),)), ctx, DiffConfig(min_order_value=3_000_000))

    assert orders == []


def test_band_of_zero_lets_every_nonzero_delta_through():
    ctx = make_ctx(closes={"100": 10_000}, positions=(make_position("100", 199),))
    cfg = DiffConfig(no_trade_band=0.0, min_order_value=0)

    orders = to_orders(Target((entry("100", 0.2),)), ctx, cfg)

    assert orders[0].qty == 1


# --- 가격·자산이 없을 때 ------------------------------------------------------


def test_entry_without_a_price_bar_is_deferred():
    ctx = make_ctx(closes={})

    assert to_orders(Target((entry("100", 0.2),)), ctx) == []


def test_zero_equity_produces_no_buy_and_no_accidental_liquidation():
    # equity를 모르면 목표 수량이 0이 되어 보유분을 통째로 팔아버릴 수 있다.
    ctx = make_ctx(closes={"100": 10_000}, equity=0, positions=(make_position("100", 200),))

    assert to_orders(Target((entry("100", 0.2),)), ctx) == []


def test_zero_equity_still_allows_an_explicit_liquidation():
    ctx = make_ctx(closes={"100": 10_000}, equity=0, positions=(make_position("100", 200),))

    orders = to_orders(Target(()), ctx)

    assert len(orders) == 1
    assert orders[0].qty == 200


def test_nonpositive_price_is_treated_as_missing():
    ctx = make_ctx(closes={"100": 0})

    assert to_orders(Target((entry("100", 0.2),)), ctx) == []


# --- 멱등 키 ------------------------------------------------------------------


def test_same_inputs_produce_identical_orders():
    ctx = make_ctx(closes={"100": 10_000})
    target = Target((entry("100", 0.2),))

    assert to_orders(target, ctx) == to_orders(target, ctx)


def test_idempotency_key_encodes_symbol_side_and_cycle():
    ctx = make_ctx(closes={"100": 10_000})

    key = to_orders(Target((entry("100", 0.2),)), ctx)[0].idempotency_key

    assert key == f"100:buy:{NOW.isoformat()}"


def test_a_later_cycle_gets_a_different_key():
    target = Target((entry("100", 0.2),))
    first = to_orders(target, make_ctx(closes={"100": 10_000}))[0]
    later = make_ctx(closes={"100": 10_000})
    later = Context(
        now=NOW + timedelta(minutes=1),
        bars=later.bars,
        watchlist=(),
        equity=EQUITY,
    )

    assert to_orders(target, later)[0].idempotency_key != first.idempotency_key


def test_keys_are_unique_within_one_cycle():
    ctx = make_ctx(
        closes={"100": 10_000, "200": 10_000},
        positions=(make_position("100", 200),),
    )

    orders = to_orders(Target((entry("200", 0.2),)), ctx)

    assert len({o.idempotency_key for o in orders}) == len(orders)


def test_event_id_is_carried_onto_orders():
    ctx = make_ctx(closes={"100": 10_000}, positions=(make_position("100", 200, event_id="E1"),))

    exit_order = to_orders(Target(()), ctx)[0]
    buy_order = to_orders(Target((entry("200", 0.2, event_id="E2"),)), make_ctx(closes={"200": 10}))

    assert exit_order.event_id == "E1"
    assert buy_order[0].event_id == "E2"


@pytest.mark.parametrize(
    "kwargs",
    [{"no_trade_band": -0.1}, {"no_trade_band": 1.0}, {"min_order_value": -1}],
)
def test_invalid_config_is_rejected(kwargs):
    with pytest.raises(ValueError):
        DiffConfig(**kwargs)

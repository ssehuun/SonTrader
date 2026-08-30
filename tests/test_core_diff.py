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
    # cash 기본값을 equity와 같게 둔다 — 포지션 없는 계좌의 정상 상태이고,
    # 매수 수량이 증거금(현금 기준)에 걸리는 것을 보려면 현금이 있어야 한다.
    base = dict(now=NOW, bars=StubBars(closes), watchlist=(), equity=equity, cash=equity)
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
        cash=EQUITY,
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


# --- 매매수량단위 (T7) ------------------------------------------------------
#
# 배수가 아닌 수량은 KIS가 거부한다. 백테스트만 체결시키면 실전에서 존재할 수
# 없는 주문이 성과에 잡힌다 — 그래서 수량을 정하는 유일한 지점에서 막는다.


def test_buy_qty_is_floored_to_the_trading_unit():
    # 자산 1,000만 × 20% = 200만 / 10,000원 = 200주. 단위 30 → 180주로 내림.
    ctx = make_ctx(closes={"100": 10_000}, trading_units={"100": 30})

    order = to_orders(Target((entry("100", 0.2),)), ctx)[0]

    assert order.qty == 180


def test_buy_is_skipped_when_flooring_leaves_nothing():
    # 목표 수량 20주인데 단위가 100주 → 살 수 있는 배수가 없다. 올림하면
    # 목표 비중을 5배 초과하므로 주문을 만들지 않는다.
    ctx = make_ctx(closes={"100": 100_000}, trading_units={"100": 100})

    assert to_orders(Target((entry("100", 0.2),)), ctx) == []


def test_partial_sell_qty_is_floored_to_the_trading_unit():
    # 보유 200주, 목표 100주 → 매도 100주. 단위 30 → 90주로 내림한다.
    # 올림하면 보유를 넘겨 잔고 부족이 되므로 방향은 항상 내림이다.
    ctx = make_ctx(
        closes={"100": 10_000},
        positions=(make_position("100", 200),),
        trading_units={"100": 30},
    )

    order = to_orders(Target((entry("100", 0.1),)), ctx)[0]

    assert order.side is Side.SELL
    assert order.qty == 90


def test_full_exit_ignores_the_trading_unit():
    # 단주가 남아도 노출은 전부 없앤다 — band·최소금액을 청산에 적용하지
    # 않는 것과 같은 논리다.
    ctx = make_ctx(
        closes={"100": 10_000},
        positions=(make_position("100", 137),),
        trading_units={"100": 100},
    )

    order = to_orders(Target(()), ctx)[0]

    assert order.qty == 137


def test_flooring_happens_before_the_min_order_value_gate():
    # 내림 전 금액은 하한을 넘지만 내림 후에는 못 넘는 경우. 판정을 나중에
    # 하지 않으면 걸러내려던 부스러기 주문이 그대로 나간다.
    # 목표 30주(300만원)를 단위 29로 내림 → 29주. 하한을 30주 금액 위로 둔다.
    ctx = make_ctx(
        closes={"100": 100_000},
        trading_units={"100": 29},
        equity=15_000_000,
    )
    config = DiffConfig(min_order_value=2_950_000, no_trade_band=0.0)

    assert to_orders(Target((entry("100", 0.2),)), ctx, config) == []


def test_unknown_or_broken_trading_unit_falls_back_to_one():
    # 마스터에 없는 종목(0/음수 포함)에서 주문이 사라지거나 나눗셈이 터지면
    # 안 된다 — 압도적으로 흔한 값 1로 진행한다.
    ctx = make_ctx(closes={"100": 10_000, "200": 10_000}, trading_units={"200": 0})

    orders = to_orders(Target((entry("100", 0.2), entry("200", 0.2))), ctx)

    assert sorted(o.qty for o in orders) == [200, 200]


# --- 기준가(ref_price) — 사후 슬리피지 측정의 근거 ---------------------------
#
# 주문 시점에 남기지 않으면 나중에 복원할 방법이 없다. 실전 체결이 아무리
# 쌓여도 기준가가 없으면 `slippage_bps` 자리표시자를 영영 검증할 수 없다.


def test_buy_order_carries_the_price_it_was_sized_from():
    ctx = make_ctx(closes={"100": 10_000})

    order = to_orders(Target((entry("100", 0.2),)), ctx)[0]

    assert order.ref_price == 10_000


def test_full_exit_carries_the_reference_price_when_a_bar_exists():
    # 청산은 IMMEDIATE 시장가라 슬리피지가 가장 크게 나는 쪽이다 — 여기서
    # 기준가가 빠지면 정작 측정하고 싶은 표본이 사라진다.
    ctx = make_ctx(closes={"100": 9_000}, positions=(make_position("100", 100),))

    order = to_orders(Target(()), ctx)[0]

    assert order.side is Side.SELL
    assert order.ref_price == 9_000


def test_exit_without_a_bar_still_goes_out_with_no_reference_price():
    # 시세 조회 실패가 청산을 막는 경로를 남기지 않는다. 측정은 부수적이다.
    ctx = make_ctx(closes={}, positions=(make_position("100", 100),))

    order = to_orders(Target(()), ctx)[0]

    assert order.qty == 100
    assert order.ref_price is None


# --- 증거금 상한 (KIS 매수가능수량) -------------------------------------------


def test_buy_qty_is_capped_by_the_margin_unit_price():
    """수량이 현재가가 아니라 **현재가 × 1.30**으로 잘린다.

    2026-08-31 실전 실측 재현 — 현금 9,447,178원 / 삼성전자 257,000원이면
    산수로는 36주지만 KIS 시장가 주문은 계산단가 334,000원 기준이라 28주다.
    """
    ctx = make_ctx(closes={"100": 257_000}, equity=9_447_178, cash=9_447_178)

    orders = to_orders(Target((entry("100", 1.0),)), ctx)

    assert len(orders) == 1
    assert orders[0].qty == 9_447_178 // (int(257_000 * 1.30) + 1)
    assert orders[0].qty == 28  # 자금이 닿는 36주가 아니다


def test_enough_cash_leaves_the_target_quantity_untouched():
    """상한에 안 걸리면 기존 동작 그대로다 — 이 수정이 평소 경로를 안 바꾼다."""
    ctx = make_ctx(closes={"100": 10_000}, equity=EQUITY, cash=EQUITY * 10)

    orders = to_orders(Target((entry("100", 0.2),)), ctx)

    assert orders[0].qty == int(EQUITY * 0.2) // 10_000


def test_a_capped_buy_ignores_the_band_and_minimum():
    """증거금에 걸려 줄어든 주문은 두 하한을 건너뛴다.

    적용하면 마지막 몇 %가 영구히 안 채워진다 — 밴드는 **가격이 움직여 생긴
    부스러기**를 막는 장치이지 사려다 못 산 잔량을 막는 장치가 아니다.
    """
    # 현금이 1주치뿐이라 잘린다. 주문 금액은 밴드·최소금액을 한참 밑돈다.
    ctx = make_ctx(closes={"100": 10_000}, equity=EQUITY, cash=13_001)

    orders = to_orders(
        Target((entry("100", 0.2),)),
        ctx,
        DiffConfig(no_trade_band=0.5, min_order_value=100_000_000),
    )

    assert [o.qty for o in orders] == [1]


def test_no_buy_without_cash():
    ctx = make_ctx(closes={"100": 10_000}, equity=EQUITY, cash=0)

    assert to_orders(Target((entry("100", 0.2),)), ctx) == []


def test_selling_is_not_constrained_by_margin():
    """매도는 현금을 쓰지 않는다. 현금 0에서도 청산이 나가야 한다 —
    시세나 잔고 사정이 노출 축소를 막는 경로를 남기지 않는다."""
    ctx = make_ctx(
        closes={"100": 10_000},
        equity=EQUITY,
        cash=0,
        positions=(make_position("100", 100),),
    )

    orders = to_orders(Target((TargetItem("100", 0.0, Urgency.IMMEDIATE),)), ctx)

    assert [(o.side, o.qty) for o in orders] == [(Side.SELL, 100)]


def test_cash_is_spent_down_across_symbols_in_one_cycle():
    """한 사이클의 매수들은 체결 전에 모두 접수되므로 증거금이 동시에 잡힌다.
    종목마다 전체 현금과 비교하면 합계가 현금을 넘는다."""
    ctx = make_ctx(
        closes={"100": 10_000, "200": 10_000, "300": 10_000},
        equity=EQUITY,
        cash=30_000,  # 계산단가 13,001원 기준 2주치뿐이다
    )

    orders = to_orders(Target((entry("100", 0.2), entry("200", 0.2), entry("300", 0.2))), ctx)

    unit = int(10_000 * 1.30) + 1
    assert sum(o.qty for o in orders) * unit <= 30_000


def test_the_next_cycle_buys_the_remainder():
    """잘린 잔량은 다음 사이클이 채운다. 별도의 재시도 장치가 필요 없는 이유다 —
    `target_qty`가 그대로라 diff가 남은 만큼을 다시 낸다."""
    target = Target((entry("100", 1.0),))
    first = to_orders(target, make_ctx(closes={"100": 10_000}, equity=EQUITY, cash=EQUITY))
    bought = first[0].qty

    # 1회차 체결 후: 현금이 줄고 포지션이 생겼다.
    spent = bought * 10_000
    second = to_orders(
        target,
        make_ctx(
            closes={"100": 10_000},
            equity=EQUITY,
            cash=EQUITY - spent,
            positions=(make_position("100", bought),),
        ),
    )

    assert second and second[0].qty > 0
    assert bought + second[0].qty > bought  # 목표에 더 가까워진다

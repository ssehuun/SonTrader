"""SimBroker 테스트 (구현 계획 5단계).

가장 중요하게 보는 것: **체결가는 항상 주문이 생성된 사이클 다음 봉의
시가**라는 것(01문서 §4.1 — 종가 즉시 체결은 look-ahead). 그 다음으로
거래비용(슬리피지·수수료·증권거래세)과 D+2 정산, 그리고 미수를 만들지 않는
현금 클램핑을 확인한다.
"""

from datetime import datetime, timedelta

import pytest

from sontrader.adapters.broker_sim import SimBroker, SimBrokerConfig
from sontrader.core.types import Bar, Order, OrderStatus, OrderType, Side, Urgency

SYMBOL = "005930"
DAY0 = datetime(2026, 3, 2)
ZERO_COST = SimBrokerConfig(commission_rate=0.0, tax_rate=0.0, slippage_bps=0.0)


def make_bar(  # noqa: A002
    symbol: str, day_offset: int, *, open: int, close: int, volume: int = 1_000
) -> Bar:
    ts = DAY0 + timedelta(days=day_offset)
    return Bar(
        symbol=symbol,
        ts=ts,
        open=open,
        high=max(open, close),
        low=min(open, close),
        close=close,
        volume=volume,
    )


def halted_bar(symbol: str, day_offset: int, *, close: int) -> Bar:
    """거래정지일 봉 — KIS가 실제로 주는 모양(거래량 0, OHLC 전부 직전 종가)."""
    return make_bar(symbol, day_offset, open=close, close=close, volume=0)


def make_order(
    symbol: str,
    side: Side,
    qty: int,
    ts: datetime,
    *,
    order_id: str | None = None,
    event_id: str | None = None,
    order_type: OrderType = OrderType.MARKET,
    limit_price: int | None = None,
) -> Order:
    return Order(
        idempotency_key=f"{symbol}:{side.value}:{ts.isoformat()}",
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=order_type,
        urgency=Urgency.NEXT_OPEN,
        ts=ts,
        limit_price=limit_price,
        event_id=event_id,
        order_id=order_id,
    )


# 봉 시가·종가를 서로 다르게 둬서 "어느 값이 체결가로 쓰였는지" 구분할 수 있게 한다.
BAR0 = make_bar(SYMBOL, 0, open=9_800, close=10_000)
BAR1 = make_bar(SYMBOL, 1, open=10_500, close=10_600)
BAR2 = make_bar(SYMBOL, 2, open=10_700, close=10_550)
SERIES = [BAR0, BAR1, BAR2]


# --- 체결가: 다음 봉의 시가 ----------------------------------------------------


def test_buy_fills_at_the_next_bars_open_not_its_own_bars_close():
    broker = SimBroker({SYMBOL: SERIES}, initial_cash=10_000_000, config=ZERO_COST)

    [result] = broker.submit([make_order(SYMBOL, Side.BUY, 10, BAR0.ts)], now=BAR0.ts)

    assert result.status is OrderStatus.FILLED
    assert result.fills[0].price == BAR1.open
    assert result.fills[0].qty == 10
    assert result.fills[0].ts == BAR1.ts


def test_sell_fills_at_the_next_bars_open():
    broker = SimBroker({SYMBOL: SERIES}, initial_cash=10_000_000, config=ZERO_COST)
    broker.submit(
        [make_order(SYMBOL, Side.BUY, 10, BAR0.ts)], now=BAR0.ts
    )  # 포지션 확보 (BAR1 시가 체결)

    [result] = broker.submit([make_order(SYMBOL, Side.SELL, 10, BAR1.ts)], now=BAR1.ts)

    assert result.status is OrderStatus.FILLED
    assert result.fills[0].price == BAR2.open


def test_order_at_a_bars_own_timestamp_does_not_fill_at_that_bar():
    broker = SimBroker({SYMBOL: SERIES}, initial_cash=10_000_000, config=ZERO_COST)

    [result] = broker.submit([make_order(SYMBOL, Side.BUY, 1, BAR1.ts)], now=BAR1.ts)

    assert result.fills[0].price == BAR2.open  # BAR1.open이 아니다


def test_order_with_no_future_bar_is_unknown_and_has_no_side_effects():
    broker = SimBroker({SYMBOL: SERIES}, initial_cash=10_000_000, config=ZERO_COST)

    [result] = broker.submit([make_order(SYMBOL, Side.BUY, 10, BAR2.ts)], now=BAR2.ts)

    assert result.status is OrderStatus.UNKNOWN
    assert result.fills == ()
    assert broker.cash() == 10_000_000
    assert broker.positions() == []


# --- 슬리피지 -------------------------------------------------------------------


def test_buy_slippage_moves_the_fill_price_up():
    config = SimBrokerConfig(commission_rate=0.0, tax_rate=0.0, slippage_bps=100.0)  # 1%
    broker = SimBroker({SYMBOL: SERIES}, initial_cash=10_000_000, config=config)

    [result] = broker.submit([make_order(SYMBOL, Side.BUY, 10, BAR0.ts)], now=BAR0.ts)

    assert result.fills[0].price == round(BAR1.open * 1.01)


def test_sell_slippage_moves_the_fill_price_down():
    config = SimBrokerConfig(commission_rate=0.0, tax_rate=0.0, slippage_bps=100.0)
    broker = SimBroker({SYMBOL: SERIES}, initial_cash=10_000_000, config=config)
    broker.submit([make_order(SYMBOL, Side.BUY, 10, BAR0.ts)], now=BAR0.ts)

    [result] = broker.submit([make_order(SYMBOL, Side.SELL, 10, BAR1.ts)], now=BAR1.ts)

    assert result.fills[0].price == round(BAR2.open * 0.99)


# --- 수수료 / 증권거래세 --------------------------------------------------------

FEE_BAR0 = make_bar("100", 0, open=9_900, close=9_900)
FEE_BAR1 = make_bar("100", 1, open=10_000, close=10_000)
FEE_BAR2 = make_bar("100", 2, open=10_000, close=10_000)
FEE_SERIES = [FEE_BAR0, FEE_BAR1, FEE_BAR2]


def test_buy_commission_is_charged_and_no_tax_applies_to_buys():
    config = SimBrokerConfig(commission_rate=0.001, tax_rate=0.01, slippage_bps=0.0)
    broker = SimBroker({"100": FEE_SERIES}, initial_cash=10_000_000, config=config)

    broker.submit([make_order("100", Side.BUY, 100, FEE_BAR0.ts)], now=FEE_BAR0.ts)

    # 가격 10,000 × 100주 = 1,000,000, 수수료 0.1% = 1,000. 세금은 매수에 없다.
    assert broker.cash() == 10_000_000 - 1_000_000 - 1_000


def test_sell_commission_reduces_proceeds():
    config = SimBrokerConfig(commission_rate=0.001, tax_rate=0.0, slippage_bps=0.0)
    broker = SimBroker({"100": FEE_SERIES}, initial_cash=10_000_000, config=config)
    broker.submit([make_order("100", Side.BUY, 100, FEE_BAR0.ts)], now=FEE_BAR0.ts)
    cash_after_buy = broker.cash()

    broker.submit([make_order("100", Side.SELL, 100, FEE_BAR1.ts)], now=FEE_BAR1.ts)
    # D+2 정산 전이라 cash는 그대로, 정산 대기액만 늘어난다.
    assert broker.cash() == cash_after_buy
    assert broker.pending_settlement == 1_000_000 - 1_000  # 매도금 - 수수료


def test_sell_tax_reduces_proceeds():
    config = SimBrokerConfig(commission_rate=0.0, tax_rate=0.002, slippage_bps=0.0)
    broker = SimBroker({"100": FEE_SERIES}, initial_cash=10_000_000, config=config)
    broker.submit([make_order("100", Side.BUY, 100, FEE_BAR0.ts)], now=FEE_BAR0.ts)

    broker.submit([make_order("100", Side.SELL, 100, FEE_BAR1.ts)], now=FEE_BAR1.ts)

    assert broker.pending_settlement == 1_000_000 - 2_000  # 매도금 - 세금


def test_total_costs_accumulates_commission_and_tax_across_both_legs():
    config = SimBrokerConfig(commission_rate=0.001, tax_rate=0.002, slippage_bps=0.0)
    broker = SimBroker({"100": FEE_SERIES}, initial_cash=10_000_000, config=config)
    assert broker.total_costs == 0

    broker.submit([make_order("100", Side.BUY, 100, FEE_BAR0.ts)], now=FEE_BAR0.ts)
    assert broker.total_costs == 1_000  # 매수 수수료만 (1,000,000 × 0.1%)

    broker.submit([make_order("100", Side.SELL, 100, FEE_BAR1.ts)], now=FEE_BAR1.ts)
    # 매도 수수료 1,000 + 세금 2,000이 더해진다.
    assert broker.total_costs == 1_000 + 1_000 + 2_000


def test_total_costs_excludes_slippage():
    config = SimBrokerConfig(commission_rate=0.0, tax_rate=0.0, slippage_bps=100.0)
    broker = SimBroker({"100": FEE_SERIES}, initial_cash=10_000_000, config=config)

    broker.submit([make_order("100", Side.BUY, 100, FEE_BAR0.ts)], now=FEE_BAR0.ts)

    assert broker.total_costs == 0


# --- 미수 방지 (현금 클램핑) -----------------------------------------------------


def test_buy_beyond_available_cash_is_rejected_outright_not_shrunk():
    """실전 KIS는 주문가능금액이 모자라면 **주문 전체를 거부**한다.

    수량을 깎아 체결시키면 실전에 존재할 수 없는 포지션이 성과에 잡힌다.
    설계도 같은 말을 한다 — "현금 부족은 주문을 줄이는 게 아니라 미루는
    문제"(`core/diff.py`). 미루는 것은 다음 사이클 diff가 알아서 한다.

    실측(2026-08-26): 이 경로로 체결되던 매수가 1,466건 중 435건(29.7%)이었다.
    """
    broker = SimBroker({SYMBOL: SERIES}, initial_cash=500_000, config=ZERO_COST)

    # BAR1 시가 10,500원 × 100주 = 1,050,000원 > 현금 500,000원.
    [result] = broker.submit([make_order(SYMBOL, Side.BUY, 100, BAR0.ts)], now=BAR0.ts)

    assert result.status is OrderStatus.REJECTED
    assert result.fills == ()
    assert broker.cash() == 500_000  # 현금은 손대지 않는다
    assert broker.positions() == []


def test_an_affordable_buy_still_fills_in_full():
    """거부는 **못 살 때만**이다. 살 수 있으면 그대로 전량 체결한다."""
    broker = SimBroker({SYMBOL: SERIES}, initial_cash=500_000, config=ZERO_COST)

    # BAR1 시가 10,500원 × 40주 = 420,000원 ≤ 500,000원.
    [result] = broker.submit([make_order(SYMBOL, Side.BUY, 40, BAR0.ts)], now=BAR0.ts)

    assert result.status is OrderStatus.FILLED
    assert result.fills[0].qty == 40


def test_buy_with_zero_affordable_quantity_is_rejected():
    broker = SimBroker({SYMBOL: SERIES}, initial_cash=100, config=ZERO_COST)

    [result] = broker.submit([make_order(SYMBOL, Side.BUY, 10, BAR0.ts)], now=BAR0.ts)

    assert result.status is OrderStatus.REJECTED
    assert result.fills == ()
    assert broker.cash() == 100
    assert broker.positions() == []


def test_buy_exactly_at_available_cash_fills_in_full():
    broker = SimBroker({SYMBOL: SERIES}, initial_cash=10 * BAR1.open, config=ZERO_COST)

    [result] = broker.submit([make_order(SYMBOL, Side.BUY, 10, BAR0.ts)], now=BAR0.ts)

    assert result.status is OrderStatus.FILLED
    assert broker.cash() == 0


# --- 매도 수량 검증 --------------------------------------------------------------


def test_selling_without_a_position_raises():
    broker = SimBroker({SYMBOL: SERIES}, initial_cash=10_000_000, config=ZERO_COST)

    with pytest.raises(ValueError, match="exceeds held qty"):
        broker.submit([make_order(SYMBOL, Side.SELL, 10, BAR0.ts)], now=BAR0.ts)


def test_selling_more_than_held_raises():
    broker = SimBroker({SYMBOL: SERIES}, initial_cash=10_000_000, config=ZERO_COST)
    broker.submit([make_order(SYMBOL, Side.BUY, 5, BAR0.ts)], now=BAR0.ts)

    with pytest.raises(ValueError, match="exceeds held qty"):
        broker.submit([make_order(SYMBOL, Side.SELL, 10, BAR1.ts)], now=BAR1.ts)


# --- 지정가 미지원 --------------------------------------------------------------


def test_limit_orders_are_not_supported():
    broker = SimBroker({SYMBOL: SERIES}, initial_cash=10_000_000, config=ZERO_COST)
    order = make_order(
        SYMBOL, Side.BUY, 10, BAR0.ts, order_type=OrderType.LIMIT, limit_price=10_000
    )

    with pytest.raises(NotImplementedError):
        broker.submit([order], now=BAR0.ts)


# --- 포지션 갱신 -----------------------------------------------------------------

AVG_BAR0 = make_bar("200", 0, open=9_000, close=9_000)
AVG_BAR1 = make_bar("200", 1, open=10_000, close=10_000)
AVG_BAR2 = make_bar("200", 2, open=20_000, close=20_000)
AVG_SERIES = [AVG_BAR0, AVG_BAR1, AVG_BAR2]


def test_repeated_buys_average_the_entry_price_by_quantity():
    broker = SimBroker({"200": AVG_SERIES}, initial_cash=10_000_000, config=ZERO_COST)

    broker.submit([make_order("200", Side.BUY, 100, AVG_BAR0.ts)], now=AVG_BAR0.ts)  # @10,000
    broker.submit([make_order("200", Side.BUY, 100, AVG_BAR1.ts)], now=AVG_BAR1.ts)  # @20,000

    [position] = broker.positions()
    assert position.qty == 200
    assert position.avg_price == pytest.approx(15_000.0)


def test_full_sell_removes_the_position():
    broker = SimBroker({SYMBOL: SERIES}, initial_cash=10_000_000, config=ZERO_COST)
    broker.submit([make_order(SYMBOL, Side.BUY, 10, BAR0.ts)], now=BAR0.ts)

    broker.submit([make_order(SYMBOL, Side.SELL, 10, BAR1.ts)], now=BAR1.ts)

    assert broker.positions() == []


def test_partial_sell_reduces_quantity_but_keeps_the_average_price():
    broker = SimBroker({SYMBOL: SERIES}, initial_cash=10_000_000, config=ZERO_COST)
    broker.submit([make_order(SYMBOL, Side.BUY, 100, BAR0.ts)], now=BAR0.ts)

    broker.submit([make_order(SYMBOL, Side.SELL, 40, BAR1.ts)], now=BAR1.ts)

    [position] = broker.positions()
    assert position.qty == 60
    assert position.avg_price == pytest.approx(BAR1.open)


# --- D+2 정산 --------------------------------------------------------------------


def test_sell_proceeds_are_not_immediately_available():
    broker = SimBroker({SYMBOL: SERIES}, initial_cash=10_000_000, config=ZERO_COST)
    broker.submit([make_order(SYMBOL, Side.BUY, 10, BAR0.ts)], now=BAR0.ts)
    cash_after_buy = broker.cash()

    broker.submit([make_order(SYMBOL, Side.SELL, 10, BAR1.ts)], now=BAR1.ts)

    assert broker.cash() == cash_after_buy
    assert broker.pending_settlement == BAR2.open * 10


def test_settlement_releases_funds_exactly_on_the_settlement_day():
    broker = SimBroker({SYMBOL: SERIES}, initial_cash=10_000_000, config=ZERO_COST)
    broker.submit([make_order(SYMBOL, Side.BUY, 10, BAR0.ts)], now=BAR0.ts)
    broker.submit([make_order(SYMBOL, Side.SELL, 10, BAR1.ts)], now=BAR1.ts)
    cash_before_settlement = broker.cash()
    settle_date = BAR2.ts + timedelta(days=2)  # D+2, 매도 체결일(BAR2.ts) 기준

    # 하루 전: 아직 정산되지 않는다.
    broker.submit([], now=settle_date - timedelta(days=1))
    assert broker.cash() == cash_before_settlement

    # 정산일 당일: 풀려난다.
    broker.submit([], now=settle_date)
    assert broker.cash() == cash_before_settlement + BAR2.open * 10
    assert broker.pending_settlement == 0


def test_settlement_advances_even_with_an_empty_order_batch():
    broker = SimBroker({SYMBOL: SERIES}, initial_cash=10_000_000, config=ZERO_COST)
    broker.submit([make_order(SYMBOL, Side.BUY, 10, BAR0.ts)], now=BAR0.ts)
    broker.submit([make_order(SYMBOL, Side.SELL, 10, BAR1.ts)], now=BAR1.ts)
    settle_date = BAR2.ts + timedelta(days=2)

    results = broker.submit([], now=settle_date)

    assert results == []
    assert broker.pending_settlement == 0


# --- 설정값 검증 ------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"commission_rate": -0.1},
        {"tax_rate": -0.1},
        {"slippage_bps": -1.0},
        {"settlement_days": -1},
    ],
)
def test_invalid_config_is_rejected(kwargs):
    with pytest.raises(ValueError):
        SimBrokerConfig(**kwargs)


def test_negative_initial_cash_is_rejected():
    with pytest.raises(ValueError):
        SimBroker({SYMBOL: SERIES}, initial_cash=-1)


# --- 거래정지 (거래량 0인 봉) -------------------------------------------------


def test_halted_bars_are_not_filled_at_and_execution_waits_for_resumption():
    """정지 중 청산은 정지 직전 가격이 아니라 **재개가**에 체결된다.

    이게 어긋나면 백테스트가 손실을 구조적으로 과소평가한다 — 정지는 보통
    악재로 걸리고 재개 시 급락하는데, 정지일 봉의 시가는 하락 반영 전
    직전 종가이기 때문이다.
    """
    bars = {
        SYMBOL: [
            make_bar(SYMBOL, 0, open=1_000, close=1_000),
            make_bar(SYMBOL, 1, open=1_000, close=1_000),
            halted_bar(SYMBOL, 2, close=1_000),
            halted_bar(SYMBOL, 3, close=1_000),
            make_bar(SYMBOL, 4, open=600, close=600),  # 재개 — 급락
        ]
    }
    broker = SimBroker(bars=bars, initial_cash=1_000_000, config=ZERO_COST)
    broker.submit([make_order(SYMBOL, Side.BUY, 10, DAY0)], now=DAY0)

    ts = DAY0 + timedelta(days=1)
    (result,) = broker.submit([make_order(SYMBOL, Side.SELL, 10, ts)], now=ts)

    assert result.status is OrderStatus.FILLED
    assert result.fills[0].price == 600  # 정지 직전 1,000이 아니라 재개가
    assert result.fills[0].ts == DAY0 + timedelta(days=4)


def test_buy_also_waits_for_resumption():
    bars = {
        SYMBOL: [
            make_bar(SYMBOL, 0, open=1_000, close=1_000),
            halted_bar(SYMBOL, 1, close=1_000),
            make_bar(SYMBOL, 2, open=1_200, close=1_200),
        ]
    }
    broker = SimBroker(bars=bars, initial_cash=1_000_000, config=ZERO_COST)

    (result,) = broker.submit([make_order(SYMBOL, Side.BUY, 10, DAY0)], now=DAY0)

    assert result.fills[0].price == 1_200


def test_settlement_is_scheduled_from_the_resumption_bar():
    """정산일도 재개일 기준이어야 한다 — 정지 중에 대금이 들어오면 안 된다."""
    bars = {
        SYMBOL: [
            make_bar(SYMBOL, 0, open=1_000, close=1_000),
            halted_bar(SYMBOL, 1, close=1_000),
            halted_bar(SYMBOL, 2, close=1_000),
            make_bar(SYMBOL, 3, open=900, close=900),
        ]
    }
    broker = SimBroker(bars=bars, initial_cash=1_000_000, config=ZERO_COST)
    broker.submit([make_order(SYMBOL, Side.BUY, 10, DAY0)], now=DAY0)
    cash_after_buy = broker.cash()

    broker.submit([make_order(SYMBOL, Side.SELL, 10, DAY0)], now=DAY0)
    assert broker.pending_settlement == 9_000

    # 재개일(D+3) + 2일 = D+5에 정산. D+4에는 아직 안 들어온다.
    broker.submit([], now=DAY0 + timedelta(days=4))
    assert broker.cash() == cash_after_buy
    broker.submit([], now=DAY0 + timedelta(days=5))
    assert broker.cash() == cash_after_buy + 9_000


def test_permanent_halt_yields_unknown_and_keeps_the_position():
    """재개 봉이 끝내 없으면(상장폐지·데이터 끝) 체결을 지어내지 않는다."""
    bars = {
        SYMBOL: [
            make_bar(SYMBOL, 0, open=1_000, close=1_000),
            halted_bar(SYMBOL, 1, close=1_000),
            halted_bar(SYMBOL, 2, close=1_000),
        ]
    }
    broker = SimBroker(bars=bars, initial_cash=1_000_000, config=ZERO_COST)
    broker.submit([make_order(SYMBOL, Side.BUY, 10, DAY0)], now=DAY0)
    assert broker.positions() == []  # 매수도 체결 못 함 — 재개 봉이 없다

    (result,) = broker.submit([make_order(SYMBOL, Side.BUY, 10, DAY0)], now=DAY0)
    assert result.status is OrderStatus.UNKNOWN


# --- 종가 동시호가 체결 (`fill_at_close`) -------------------------------------
#
# 15:20 종가 동시호가로 집행하는 모드다. 기본(시가 체결)과 **같은 주문이 다른
# 봉·다른 가격에 체결**되므로, 여기서 고정하는 것은 "어느 봉인가"와 "어느
# 가격인가" 둘이다.

CLOSE_FILL = SimBrokerConfig(
    commission_rate=0.0, tax_rate=0.0, slippage_bps=0.0, fill_at_close=True
)


def test_close_fill_uses_the_signal_bar_itself():
    """다음 봉이 아니라 **주문이 난 그 봉**의 종가에 체결한다."""
    bars = {
        SYMBOL: [
            make_bar(SYMBOL, 0, open=900, close=1_000),
            make_bar(SYMBOL, 1, open=1_500, close=1_600),
        ]
    }
    broker = SimBroker(bars=bars, initial_cash=1_000_000, config=CLOSE_FILL)

    (result,) = broker.submit([make_order(SYMBOL, Side.BUY, 10, DAY0)], now=DAY0)

    assert result.status is OrderStatus.FILLED
    (fill,) = result.fills
    assert fill.ts == DAY0  # 다음 봉(DAY0+1)이 아니다
    assert fill.price == 1_000  # 그 봉의 시가 900도, 다음 봉 시가 1,500도 아니다


def test_close_fill_keeps_slippage_on_the_unfavorable_side():
    """슬리피지 기준가만 시가→종가로 바뀐다. 방향은 그대로 불리한 쪽이다."""
    bars = {SYMBOL: [make_bar(SYMBOL, 0, open=900, close=1_000)]}
    config = SimBrokerConfig(
        commission_rate=0.0, tax_rate=0.0, slippage_bps=100.0, fill_at_close=True
    )
    broker = SimBroker(bars=bars, initial_cash=1_000_000, config=config)

    (bought,) = broker.submit([make_order(SYMBOL, Side.BUY, 10, DAY0)], now=DAY0)
    (sold,) = broker.submit([make_order(SYMBOL, Side.SELL, 10, DAY0)], now=DAY0)

    assert bought.fills[0].price == 1_010  # 종가 × 1.01
    assert sold.fills[0].price == 990  # 종가 × 0.99


def test_close_fill_skips_halted_bars_like_the_open_path():
    """거래정지 봉(거래량 0)에는 체결하지 않는다 — 시가 경로와 같은 규칙이다."""
    bars = {
        SYMBOL: [
            halted_bar(SYMBOL, 0, close=1_000),
            make_bar(SYMBOL, 1, open=1_100, close=1_200),
        ]
    }
    broker = SimBroker(bars=bars, initial_cash=1_000_000, config=CLOSE_FILL)

    (result,) = broker.submit([make_order(SYMBOL, Side.BUY, 10, DAY0)], now=DAY0)

    assert result.fills[0].price == 1_200  # 재개 봉의 종가


def test_open_fill_remains_the_default():
    """기본값을 바꾸지 않는다 — 종목 전략·기준선이 시가 체결로 잰 값이다."""
    assert SimBrokerConfig().fill_at_close is False

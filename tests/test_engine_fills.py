"""체결 → 포지션 변경 규칙 테스트 (`engine/fills.py`).

이 모듈에는 직접 테스트가 없었고, 그 틈으로 규칙 2 버그가 오래 살아남았다:
**"주문이 전량 체결됐는가"를 "포지션이 비었는가"로 착각**해서, 드리프트
리밸런싱 트림(18주 중 9주 매도)이 청산으로 잡혔다. 그래서 여기서 가장
무게를 두는 것은 **트림과 청산의 구분**이다.
"""

from datetime import datetime

from sontrader.adapters.broker import OrderResult
from sontrader.core.types import ExitRule, Fill, Order, OrderStatus, OrderType, Side, Urgency
from sontrader.engine import fills

NOW = datetime(2026, 8, 26, 9, 0)


def make_result(
    symbol: str,
    side: Side,
    qty: int,
    *,
    filled: int | None = None,
    price: int = 10_000,
    status: OrderStatus = OrderStatus.FILLED,
    exit_rule: ExitRule | None = None,
) -> OrderResult:
    filled = qty if filled is None else filled
    order = Order(
        idempotency_key=f"{symbol}:{side.value}:{NOW.isoformat()}",
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=OrderType.MARKET,
        urgency=Urgency.NEXT_OPEN,
        ts=NOW,
        exit_rule=exit_rule,
    )
    return OrderResult(
        order=order,
        status=status,
        fills=(Fill(order_id="o1", price=price, qty=filled, ts=NOW),),
    )


# --- 규칙 2: 잔량이 0일 때만 닫는다 ------------------------------------------


def test_trimming_a_position_does_not_close_it():
    """18주 보유 중 9주 매도 — 주문은 전량체결이지만 포지션은 살아 있다.

    이것이 고아 포지션 버그의 정확한 재현이다. 닫아버리면 남은 9주에
    청산 규칙이 영영 적용되지 않고, 게이트의 슬롯 계산에서도 사라진다.
    """
    changes = fills.position_changes([make_result("100", Side.SELL, 9)], held={"100": 18})

    assert changes == []


def test_selling_the_entire_position_closes_it():
    changes = fills.position_changes([make_result("100", Side.SELL, 18)], held={"100": 18})

    assert len(changes) == 1
    assert isinstance(changes[0], fills.Closed)
    assert changes[0].symbol == "100"
    assert changes[0].qty == 18


def test_partially_filled_full_exit_keeps_the_position():
    """전량 청산 주문이 일부만 체결되면 잔량이 남아 있으므로 닫지 않는다."""
    changes = fills.position_changes(
        [make_result("100", Side.SELL, 18, filled=5, status=OrderStatus.PARTIAL)],
        held={"100": 18},
    )

    assert changes == []


def test_two_trims_in_sequence_close_only_when_the_position_empties():
    """한 사이클에 여러 매도가 들어와도 잔량이 일관되게 누적돼야 한다."""
    changes = fills.position_changes(
        [make_result("100", Side.SELL, 10), make_result("100", Side.SELL, 8)],
        held={"100": 18},
    )

    assert len(changes) == 1
    assert isinstance(changes[0], fills.Closed)


# --- 규칙 1: 진입 정보는 보유 중 바뀌지 않는다 -------------------------------


def test_a_new_buy_opens_a_position():
    rule = ExitRule(max_hold_days=7)
    [change] = fills.position_changes(
        [make_result("100", Side.BUY, 10, price=9_500, exit_rule=rule)], held={}
    )

    assert isinstance(change, fills.Opened)
    assert change.qty == 10
    assert change.entry_price == 9_500
    assert change.exit_rule == rule


def test_adding_to_an_existing_position_does_not_reopen_it():
    changes = fills.position_changes([make_result("100", Side.BUY, 5)], held={"100": 10})

    assert changes == []


def test_a_top_up_buy_counts_toward_the_remaining_quantity():
    """추가 매수를 잔량에 반영하지 않으면 뒤따르는 매도가 조기에 청산으로 잡힌다."""
    changes = fills.position_changes(
        [make_result("100", Side.BUY, 5), make_result("100", Side.SELL, 10)],
        held={"100": 10},
    )

    # 10 + 5 − 10 = 5주가 남는다 → 청산 아님.
    assert changes == []


def test_buy_then_full_sell_within_one_cycle_opens_and_closes():
    changes = fills.position_changes(
        [make_result("100", Side.BUY, 10), make_result("100", Side.SELL, 10)],
        held={},
    )

    assert [type(c) for c in changes] == [fills.Opened, fills.Closed]


# --- 무시하는 것 ------------------------------------------------------------


def test_unfilled_results_are_ignored():
    changes = fills.position_changes(
        [make_result("100", Side.BUY, 10, status=OrderStatus.REJECTED)], held={}
    )

    assert changes == []

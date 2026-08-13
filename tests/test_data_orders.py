"""주문/체결 영속화 테스트 (구현 계획 7단계).

가장 중요하게 보는 것: (1) 같은 idempotency_key는 DB 수준에서 막힌다
(멱등성의 2차 방어선), (2) `list_unresolved()`가 재시작 복구 대상을
정확히 골라낸다.
"""

from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from sontrader.core.types import Fill, Order, OrderStatus, OrderType, Side, Urgency
from sontrader.data import db, orders

NOW = datetime(2026, 3, 10, 9, 30)


def seed_event(engine, event_id: str = "E1") -> None:
    with engine.begin() as conn:
        conn.execute(
            db.events.insert().values(
                event_id=event_id,
                symbol="005930",
                corp_code="00126380",
                event_type="earnings",
                norm_key=f"key:{event_id}",
                title="공시",
                published_at=NOW,
                ingested_at=NOW,
                raw_json={},
            )
        )


def make_order(
    symbol: str = "005930",
    *,
    side: Side = Side.BUY,
    qty: int = 100,
    event_id: str | None = None,
    idempotency_key: str | None = None,
) -> Order:
    return Order(
        idempotency_key=idempotency_key or f"{symbol}:{side.value}:{NOW.isoformat()}",
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=OrderType.MARKET,
        urgency=Urgency.NEXT_OPEN,
        ts=NOW,
        event_id=event_id,
    )


# --- 멱등성 -----------------------------------------------------------------------


def test_find_by_idempotency_key_returns_none_when_not_found(db_engine):
    db.migrate(db_engine)

    assert orders.find_by_idempotency_key(db_engine, "no-such-key") is None


def test_insert_then_find_by_idempotency_key_roundtrips(db_engine):
    db.migrate(db_engine)
    order = make_order(qty=200)

    orders.insert(db_engine, order, order_id="ord-1", status=OrderStatus.SUBMITTED, created_at=NOW)
    record = orders.find_by_idempotency_key(db_engine, order.idempotency_key)

    assert record is not None
    assert record.order_id == "ord-1"
    assert record.symbol == "005930"
    assert record.side is Side.BUY
    assert record.qty == 200
    assert record.status is OrderStatus.SUBMITTED
    assert record.broker_order_no is None
    assert record.event_id is None


def test_insert_rejects_duplicate_idempotency_key(db_engine):
    # DB의 UNIQUE 제약 — 멱등성의 2차 방어선. 호출자가 먼저 확인을
    # 빼먹어도 중복 제출이 조용히 통과하지 않는다.
    db.migrate(db_engine)
    order = make_order()
    orders.insert(db_engine, order, order_id="ord-1", status=OrderStatus.SUBMITTED, created_at=NOW)

    duplicate = make_order(idempotency_key=order.idempotency_key)
    with pytest.raises(IntegrityError):
        orders.insert(
            db_engine, duplicate, order_id="ord-2", status=OrderStatus.SUBMITTED, created_at=NOW
        )


def test_insert_with_event_id_requires_a_matching_event(db_engine):
    db.migrate(db_engine)
    seed_event(db_engine, "E1")
    order = make_order(event_id="E1")

    orders.insert(db_engine, order, order_id="ord-1", status=OrderStatus.SUBMITTED, created_at=NOW)
    record = orders.find_by_idempotency_key(db_engine, order.idempotency_key)

    assert record.event_id == "E1"


def test_get_returns_the_order_by_order_id(db_engine):
    db.migrate(db_engine)
    order = make_order()
    orders.insert(db_engine, order, order_id="ord-1", status=OrderStatus.SUBMITTED, created_at=NOW)

    assert orders.get(db_engine, "ord-1").idempotency_key == order.idempotency_key
    assert orders.get(db_engine, "no-such-id") is None


# --- 상태 갱신 --------------------------------------------------------------------


def test_update_status_changes_status_only(db_engine):
    db.migrate(db_engine)
    order = make_order()
    orders.insert(db_engine, order, order_id="ord-1", status=OrderStatus.UNKNOWN, created_at=NOW)

    orders.update_status(db_engine, "ord-1", OrderStatus.FILLED)

    record = orders.get(db_engine, "ord-1")
    assert record.status is OrderStatus.FILLED
    assert record.broker_order_no is None


def test_update_status_sets_broker_order_no_when_given(db_engine):
    db.migrate(db_engine)
    order = make_order()
    orders.insert(db_engine, order, order_id="ord-1", status=OrderStatus.UNKNOWN, created_at=NOW)

    orders.update_status(db_engine, "ord-1", OrderStatus.FILLED, broker_order_no="0000123456")

    assert orders.get(db_engine, "ord-1").broker_order_no == "0000123456"


def test_update_status_does_not_clear_broker_order_no_when_omitted(db_engine):
    db.migrate(db_engine)
    order = make_order()
    orders.insert(
        db_engine,
        order,
        order_id="ord-1",
        status=OrderStatus.SUBMITTED,
        created_at=NOW,
        broker_order_no="0000123456",
    )

    orders.update_status(db_engine, "ord-1", OrderStatus.PARTIAL)

    assert orders.get(db_engine, "ord-1").broker_order_no == "0000123456"


# --- 체결 ---------------------------------------------------------------------------


def test_record_fills_then_load_fills_roundtrips(db_engine):
    db.migrate(db_engine)
    order = make_order()
    orders.insert(db_engine, order, order_id="ord-1", status=OrderStatus.PARTIAL, created_at=NOW)
    fills = [
        Fill(order_id="ord-1", price=10_000, qty=50, ts=NOW),
        Fill(order_id="ord-1", price=10_050, qty=50, ts=NOW),
    ]

    orders.record_fills(db_engine, "ord-1", fills)
    loaded = orders.load_fills(db_engine, "ord-1")

    assert loaded == fills


def test_record_fills_with_empty_list_is_a_noop(db_engine):
    db.migrate(db_engine)
    order = make_order()
    orders.insert(db_engine, order, order_id="ord-1", status=OrderStatus.SUBMITTED, created_at=NOW)

    orders.record_fills(db_engine, "ord-1", [])

    assert orders.load_fills(db_engine, "ord-1") == []


# --- 재시작 복구 ----------------------------------------------------------------------


def test_list_unresolved_returns_submitted_unknown_and_partial(db_engine):
    db.migrate(db_engine)
    for i, status in enumerate(
        [OrderStatus.SUBMITTED, OrderStatus.UNKNOWN, OrderStatus.PARTIAL], start=1
    ):
        order = make_order(idempotency_key=f"key-{i}")
        orders.insert(db_engine, order, order_id=f"ord-{i}", status=status, created_at=NOW)

    unresolved = orders.list_unresolved(db_engine)

    assert {r.order_id for r in unresolved} == {"ord-1", "ord-2", "ord-3"}


def test_list_unresolved_excludes_terminal_statuses(db_engine):
    db.migrate(db_engine)
    for i, status in enumerate(
        [OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED], start=1
    ):
        order = make_order(idempotency_key=f"key-{i}")
        orders.insert(db_engine, order, order_id=f"ord-{i}", status=status, created_at=NOW)

    assert orders.list_unresolved(db_engine) == []

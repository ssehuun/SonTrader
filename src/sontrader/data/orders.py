"""주문/체결 영속화 — 멱등성의 2차 방어선 (구현 계획 7단계).

01문서 §2.6: "멱등 키로 중복 주문 차단", "프로세스 재시작 시 미체결 주문
복구". `core.types.Order.idempotency_key`가 1차 방어선(같은 사이클에서
같은 주문을 두 번 안 만든다)이고, 여기 `orders.idempotency_key`의 UNIQUE
제약이 2차 방어선이다 — 프로세스가 재시작되거나 네트워크 재시도로 같은
주문이 다시 제출돼도, 이미 기록된 이력이 있으면 그걸 반환하고 실제 제출은
건너뛴다.

이 모듈은 DB 읽기/쓰기만 한다 — KIS API 호출은 `adapters/broker_kis.py`
(다음 슬라이스)의 몫이다. `list_unresolved()`가 재시작 복구의 진입점이다:
`SUBMITTED`/`UNKNOWN` 상태로 남아 있는 주문은 재시작 후 KIS에 조회해서
실제 체결 여부를 확인해야 한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.core.types import ExitRule, Fill, Order, OrderStatus, OrderType, Side, Urgency
from sontrader.data import db

# 재시작 시 결과를 다시 확인해야 하는 상태 — 아직 최종 상태(체결/거부/취소)로
# 확정되지 않았다.
UNRESOLVED_STATUSES = (OrderStatus.SUBMITTED, OrderStatus.UNKNOWN, OrderStatus.PARTIAL)


@dataclass(frozen=True)
class OrderRecord:
    order_id: str
    idempotency_key: str
    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    urgency: Urgency
    status: OrderStatus
    event_id: str | None
    broker_order_no: str | None
    created_at: datetime
    exit_rule: ExitRule | None = None
    # 의사결정 시점의 기준가. 체결가와의 차이가 실측 슬리피지가 된다
    # (`apps/slippage.py`). 옛 행에는 없어 None일 수 있다.
    ref_price: int | None = None


def find_by_idempotency_key(engine: Engine, idempotency_key: str) -> OrderRecord | None:
    """이미 제출된 적 있는 주문인지 확인한다 — 멱등성의 2차 방어선."""
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(db.orders).where(db.orders.c.idempotency_key == idempotency_key)
        ).first()
    return _to_record(row) if row is not None else None


def get(engine: Engine, order_id: str) -> OrderRecord | None:
    with engine.connect() as conn:
        row = conn.execute(sa.select(db.orders).where(db.orders.c.order_id == order_id)).first()
    return _to_record(row) if row is not None else None


def insert(
    engine: Engine,
    order: Order,
    *,
    order_id: str,
    status: OrderStatus,
    created_at: datetime,
    broker_order_no: str | None = None,
) -> None:
    """새 주문 제출을 기록한다. 같은 idempotency_key가 이미 있으면 DB의
    UNIQUE 제약이 막는다 — 조용히 넘어가지 않고 예외로 드러난다(fail-closed;
    호출자는 먼저 `find_by_idempotency_key()`로 확인해야 한다)."""
    with engine.begin() as conn:
        conn.execute(
            db.orders.insert().values(
                order_id=order_id,
                idempotency_key=order.idempotency_key,
                symbol=order.symbol,
                side=order.side.value,
                qty=order.qty,
                order_type=order.order_type.value,
                urgency=order.urgency.value,
                status=status.value,
                event_id=order.event_id,
                broker_order_no=broker_order_no,
                created_at=created_at,
                exit_rule_json=order.exit_rule.to_dict() if order.exit_rule else None,
                ref_price=order.ref_price,
            )
        )


def update_status(
    engine: Engine, order_id: str, status: OrderStatus, *, broker_order_no: str | None = None
) -> None:
    """상태를 갱신한다. `broker_order_no`는 넘겼을 때만 덮어쓴다 — 접수
    불명(UNKNOWN) 상태로 기록했다가 나중에 조회로 ODNO를 알게 되는 경우를
    위해서다."""
    values: dict[str, object] = {"status": status.value}
    if broker_order_no is not None:
        values["broker_order_no"] = broker_order_no
    with engine.begin() as conn:
        conn.execute(sa.update(db.orders).where(db.orders.c.order_id == order_id).values(**values))


def record_fills(engine: Engine, order_id: str, fills: Sequence[Fill]) -> None:
    if not fills:
        return
    rows = [{"order_id": order_id, "price": f.price, "qty": f.qty, "ts": f.ts} for f in fills]
    with engine.begin() as conn:
        conn.execute(db.fills.insert(), rows)


def set_fill_snapshot(engine: Engine, order_id: str, fill: Fill) -> None:
    """이 주문의 체결 상태를 `fill` 하나로 완전히 대체한다.

    KIS 일별주문체결조회(`adapters/broker_kis.py`)는 개별 체결 이벤트가
    아니라 누적 체결수량·평균단가만 준다 — 그래서 여러 번 조회해도
    "지금까지 누적된 체결"이라는 스냅샷 하나만 표현할 수 있다.
    `record_fills()`처럼 계속 추가(append)하면 조회할 때마다 중복
    합산되므로, 여기서는 기존 체결 기록을 지우고 최신 스냅샷 하나로
    교체한다.
    """
    with engine.begin() as conn:
        conn.execute(sa.delete(db.fills).where(db.fills.c.order_id == order_id))
        conn.execute(
            db.fills.insert().values(order_id=order_id, price=fill.price, qty=fill.qty, ts=fill.ts)
        )


def load_fills(engine: Engine, order_id: str) -> list[Fill]:
    columns = db.fills.c
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(columns.price, columns.qty, columns.ts)
            .where(columns.order_id == order_id)
            .order_by(columns.ts)
        )
        return [Fill(order_id=order_id, price=row.price, qty=row.qty, ts=row.ts) for row in rows]


def list_unresolved(engine: Engine) -> list[OrderRecord]:
    """재시작 복구 진입점 — 아직 최종 상태가 아닌 주문 전부.

    01문서 §2.6 "프로세스 재시작 시 미체결 주문 복구"의 기반. 호출자
    (`adapters/broker_kis.py`)는 각 건을 KIS에 조회해 실제 상태로 갱신한다.
    """
    statuses = [s.value for s in UNRESOLVED_STATUSES]
    with engine.connect() as conn:
        rows = conn.execute(sa.select(db.orders).where(db.orders.c.status.in_(statuses))).all()
    return [_to_record(row) for row in rows]


def _to_record(row) -> OrderRecord:
    return OrderRecord(
        order_id=row.order_id,
        idempotency_key=row.idempotency_key,
        symbol=row.symbol,
        side=Side(row.side),
        qty=row.qty,
        order_type=OrderType(row.order_type),
        urgency=Urgency(row.urgency),
        status=OrderStatus(row.status),
        event_id=row.event_id,
        broker_order_no=row.broker_order_no,
        created_at=row.created_at,
        exit_rule=ExitRule.from_dict(row.exit_rule_json) if row.exit_rule_json else None,
        ref_price=row.ref_price,
    )

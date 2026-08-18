"""Broker 프로토콜 (구현 계획 5단계). 02문서 §3.5.

`core.types.Order`를 받아 체결을 시도하고, 우리 쪽이 알아야 할 결과만
돌려준다. 실전 구현(`broker_kis.py`, 7단계 미착수)과 시뮬레이션 구현
(`broker_sim.py`, 이번 슬라이스)이 이 프로토콜 하나를 공유한다.

`positions()`가 반환하는 것은 core의 `Position`이 아니라 여기 정의된
`BrokerPosition`(수량·평단가)이다. 거래소는 우리가 진입 시점에 확정한 청산
조건(`ExitRule`)이나 어느 이벤트로 들어왔는지(`event_id`)를 모른다 — 그건
우리 DB에만 있는 상태다. 브로커가 아는 것(수량·평단가)과 우리 DB에 저장된
전략 상태를 합쳐 `core.types.Position`을 재구성하는 일은
`engine/reconcile.py`의 몫이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sontrader.core.types import Fill, Order, OrderStatus


@dataclass(frozen=True)
class BrokerPosition:
    """브로커(거래소)가 아는 만큼만 — 수량과 평단가."""

    symbol: str
    qty: int
    avg_price: float


@dataclass(frozen=True)
class OrderResult:
    """제출 한 건의 결과 (설계 2.6절).

    `status=UNKNOWN`은 접수 여부를 알 수 없는 경우다 — 실전은 API 타임아웃,
    시뮬레이션은 체결에 쓸 미래 봉이 데이터 끝이라 없는 경우다. `fills`는 이
    제출에서 즉시 확인된 체결만 담으며, 없을 수 있다.
    """

    order: Order
    status: OrderStatus
    fills: tuple[Fill, ...] = ()
    broker_order_no: str | None = None  # KIS 주문번호(ODNO) 등 — UNKNOWN 해소용
    # 거절 사유 원문. "잔고 부족"과 "유량 초과"는 대응이 완전히 다른데,
    # 상태 코드만으로는 구분할 수 없어 상시 가동에서 관측 공백이 된다.
    reason: str | None = None


class Broker(Protocol):
    def submit(self, orders: list[Order], *, now: datetime) -> list[OrderResult]:
        """주문을 제출한다.

        `now`는 이 호출이 속한 사이클의 시각이다. 주문이 하나도 없는
        사이클에도 매번 불러야 한다 — 시뮬레이션 구현은 이 호출을 신호 삼아
        D+2 정산을 진행하므로, 호출이 끊기면 정산도 멈춘다.
        """
        ...

    def positions(self) -> list[BrokerPosition]: ...

    def cash(self) -> int:
        """정산 완료되어 사용 가능한 현금. D+2 대기 중인 매도 대금은 제외한다."""
        ...

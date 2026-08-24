"""KIS 실전 브로커 (구현 계획 7단계). `Broker` 프로토콜 구현체 —
`broker_sim.py`와 짝을 이룬다.

02문서 §3.5가 `broker_kis`의 책임으로 명시한 것: 유량 제한(이미
`client.py`가 재시도·페이싱을 담당), **멱등 키**(같은 키 재전송해도 중복
체결 안 됨), **타임아웃 시 "접수 불명" 상태**, 그리고 그 접수 불명을
나중에 해소하는 것.

## 왜 KIS를 부르기 *전에* UNKNOWN으로 먼저 기록하는가

`_submit_one()`은 `data/orders.py`에 주문을 남기는 시점이 KIS 호출 *이전*
이다. 그 반대로 하면(호출 후 결과가 왔을 때만 기록) 호출과 기록 사이에
프로세스가 죽었을 때 아무 흔적도 안 남는다 — 재시작해도 이 주문을 다시
만들 이유(idempotency_key 재사용)가 없으니 그대로 재제출되고, 만약 KIS가
실제로는 접수했었다면 중복 주문이 나간다. 먼저 UNKNOWN으로 선점해두면
크래시가 나도 `list_unresolved()`가 이 건을 집어내 다음 기동 때 조회
대상에 올린다(재시작 복구, 01문서 §2.6).

## 접수 불명 해소: `resolve_unknown()`

`submit()`이 접수만 확인하고 실제 체결은 모르는 채로 남긴 주문(`SUBMITTED`)
과, 접수 자체가 불확실한 주문(`UNKNOWN`)을 `주식일별주문체결조회`
(`client.get_daily_executions`, TR_ID TTTC0081R/VTTC0081R — 3개월 이내)로
확인한다. `broker_order_no`(ODNO)가 없는 건은 이 API로 특정할 방법이
없어 건너뛴다 — ODNO 없이는 같은 종목·같은 날 여러 주문 중 어느 것인지
구분할 수 없다. 이런 건은 사람이 KIS 앱/HTS에서 직접 확인해야 한다.

이 API는 개별 체결 이벤트가 아니라 **누적** 체결수량·평균단가만 주므로,
조회할 때마다 체결 스냅샷 전체를 다시 기록한다(`data/orders.py`의
`set_fill_snapshot` — append가 아니라 교체).

`resolve_unknown()`은 이 클래스가 스스로 호출하지 않는다 — 언제(매 사이클
시작 시인지, 재시작 시인지) 부를지는 엔진의 정책이라 별도 메서드로 남겨
두고, 호출 시점은 `engine/reconcile.py`(9단계, 미착수)의 몫으로 둔다.

`positions()`/`cash()`는 이미 검증된 잔고조회(`get_balance`,
TTTC8434R/VTTC8434R)를 그대로 쓴다.
"""

from __future__ import annotations

import logging
import time as time_module
import uuid
from collections.abc import Callable
from datetime import datetime

import httpx
from sqlalchemy.engine import Engine

from sontrader.adapters.broker import BrokerPosition, OrderResult
from sontrader.client import KisClient, KisError
from sontrader.core.types import Fill, Order, OrderStatus, Side
from sontrader.data import orders as orders_repo
from sontrader.data.orders import OrderRecord
from sontrader.logging_setup import traced

log = logging.getLogger(__name__)

_SUBMIT_RETRIES = 3


class KisBroker:
    def __init__(
        self,
        client: KisClient,
        engine: Engine,
        *,
        sleep: Callable[[float], None] = time_module.sleep,
    ) -> None:
        self._client = client
        self._engine = engine
        self._sleep = sleep
        # ODNO 없는 주문을 이미 경고한 order_id. `list_unresolved()`는 나이
        # 제한이 없고 이 주문들은 상태가 영영 바뀌지 않으므로, 조건 없이 찍으면
        # 하루 약 390회 같은 경고가 반복되어 정작 조치가 필요한 WARNING을
        # 묻어 버린다. 프로세스 수명 동안만 기억하면 충분하다.
        self._warned_missing_odno: set[str] = set()

    @traced
    def submit(self, orders: list[Order], *, now: datetime) -> list[OrderResult]:
        return [self._submit_one(order, now) for order in orders]

    @traced
    def positions(self) -> list[BrokerPosition]:
        balance = self._client.get_balance()
        result = []
        for holding in balance["holdings"]:
            qty = int(holding["hldg_qty"])
            if qty <= 0:
                continue
            result.append(
                BrokerPosition(
                    symbol=holding["pdno"], qty=qty, avg_price=float(holding["pchs_avg_pric"])
                )
            )
        return result

    @traced
    def cash(self) -> int:
        balance = self._client.get_balance()
        return int(balance["summary"].get("dnca_tot_amt", 0))

    @traced
    def resolve_unknown(self) -> list[OrderResult]:
        """SUBMITTED/UNKNOWN/PARTIAL로 남은 주문을 KIS에 조회해 확정한다."""
        results = []
        for record in orders_repo.list_unresolved(self._engine):
            if record.broker_order_no is None:
                # 자동으로 영원히 해소되지 않는다 — 사람이 KIS 앱에서 확인해야
                # 한다. 조용히 넘기면 미확정 주문이 무한히 쌓인다.
                if record.order_id not in self._warned_missing_odno:
                    self._warned_missing_odno.add(record.order_id)
                    log.warning(
                        "주문 %s (%s %s %d주, %s)에 ODNO가 없어 확인 불가 — "
                        "KIS 앱에서 직접 확인 필요 (이 주문은 한 번만 알립니다)",
                        record.order_id,
                        record.symbol,
                        record.side.value,
                        record.qty,
                        record.status.value,
                    )
                continue
            results.append(self._resolve_one(record))
        return results

    # --- 내부 ---------------------------------------------------------------

    def _submit_one(self, order: Order, now: datetime) -> OrderResult:
        existing = orders_repo.find_by_idempotency_key(self._engine, order.idempotency_key)
        if existing is not None:
            # 이미 제출된 적 있는 주문 — 재전송이어도 다시 쏘지 않는다
            # (멱등 키 1차 방어선 + DB UNIQUE 제약 2차 방어선, 설계 2.6절).
            log.info(
                "중복 주문 차단 %s %s %d주 — 기존 %s (%s)",
                order.symbol,
                order.side.value,
                order.qty,
                existing.order_id,
                existing.status.value,
            )
            fills = orders_repo.load_fills(self._engine, existing.order_id)
            return OrderResult(
                order=order,
                status=existing.status,
                fills=tuple(fills),
                broker_order_no=existing.broker_order_no,
            )

        order_id = str(uuid.uuid4())
        orders_repo.insert(
            self._engine, order, order_id=order_id, status=OrderStatus.UNKNOWN, created_at=now
        )

        side = "buy" if order.side is Side.BUY else "sell"
        try:
            response = self._client.order(side, order.symbol, order.qty, price=order.limit_price)
        except httpx.HTTPError as exc:
            # 접수 여부를 알 수 없다 — 이미 UNKNOWN으로 기록돼 있으니 그대로 둔다.
            # ERROR가 아니라 WARNING인 이유: resolve_unknown()이 다음 사이클에
            # 확정한다. 다만 실제로 접수됐을 수 있어 조용히 넘기면 안 된다.
            log.warning(
                "주문 접수 불명 %s %s %d주 (%s: %s) — order_id=%s, 다음 사이클에 조회",
                order.symbol,
                side,
                order.qty,
                type(exc).__name__,
                exc,
                order_id,
            )
            return OrderResult(order=order, status=OrderStatus.UNKNOWN)
        except KisError as exc:
            # 사유를 남긴다. 예전에는 그냥 REJECTED로만 기록해서 "잔고 부족"과
            # "유량 초과"를 구분할 수 없었다 — 상시 가동에서 관측 공백이 된다.
            orders_repo.update_status(self._engine, order_id, OrderStatus.REJECTED)
            log.error("주문 거절 %s %s %s주: %s", order.symbol, side, order.qty, exc)
            return OrderResult(order=order, status=OrderStatus.REJECTED, reason=str(exc))

        broker_order_no = response.get("ODNO")
        orders_repo.update_status(
            self._engine, order_id, OrderStatus.SUBMITTED, broker_order_no=broker_order_no
        )
        # 실제 자금이 움직이기 시작한 지점이다. ODNO를 남겨야 나중에 KIS
        # 앱/HTS에서 같은 주문을 찾아 대조할 수 있다.
        log.info(
            "주문 제출 %s %s %d주 %s → ODNO=%s",
            order.symbol,
            side,
            order.qty,
            "시장가" if order.limit_price is None else f"@{order.limit_price:,}",
            broker_order_no,
        )
        return OrderResult(
            order=order, status=OrderStatus.SUBMITTED, broker_order_no=broker_order_no
        )

    def _resolve_one(self, record: OrderRecord) -> OrderResult:
        order = _order_from_record(record)
        day = record.created_at.date()
        rows = self._client.get_daily_executions(
            day, day, symbol=record.symbol, broker_order_no=record.broker_order_no
        )
        if not rows:
            # 아직 KIS 쪽에 반영 전이거나 못 찾음 — 상태를 바꾸지 않고
            # 다음 사이클에 다시 시도한다.
            return OrderResult(
                order=order, status=record.status, broker_order_no=record.broker_order_no
            )

        row = rows[0]
        ord_qty = int(row["ord_qty"])
        filled_qty = int(row["tot_ccld_qty"])
        rejected_qty = int(row.get("rjct_qty") or 0)
        cancelled = (row.get("cncl_yn") or "").strip().upper() == "Y"

        if cancelled:
            status = OrderStatus.CANCELLED
        elif filled_qty >= ord_qty:
            status = OrderStatus.FILLED
        elif filled_qty > 0:
            status = OrderStatus.PARTIAL
        elif rejected_qty >= ord_qty:
            status = OrderStatus.REJECTED
        else:
            # 이 행이 존재한다는 것 자체가 KIS가 접수했다는 뜻이다 — UNKNOWN
            # 이었다면 최소한 SUBMITTED로는 확정할 수 있다.
            status = OrderStatus.SUBMITTED

        fills: tuple[Fill, ...] = ()
        if filled_qty > 0:
            fill_ts = datetime.strptime(row["ord_dt"] + row["ord_tmd"], "%Y%m%d%H%M%S")
            fill = Fill(
                order_id=record.order_id,
                price=round(float(row["avg_prvs"])),
                qty=filled_qty,
                ts=fill_ts,
            )
            orders_repo.set_fill_snapshot(self._engine, record.order_id, fill)
            fills = (fill,)

        orders_repo.update_status(self._engine, record.order_id, status)
        if status != record.status:
            # 상태가 바뀐 것만 남긴다. 안 바뀌면 매 사이클 같은 줄이 반복된다.
            # 이미 저장한 값(`set_fill_snapshot`)을 그대로 쓴다. row를 다시
            # 파싱하면 로그와 DB가 어긋날 여지가 생긴다.
            detail = f" {fills[0].qty}주 @{fills[0].price:,}" if fills else ""
            log.info(
                "주문 확정 %s %s → %s%s (ODNO=%s)",
                record.symbol,
                record.status.value,
                status.value,
                detail,
                record.broker_order_no,
            )
        return OrderResult(
            order=order, status=status, fills=fills, broker_order_no=record.broker_order_no
        )


def _order_from_record(record: OrderRecord) -> Order:
    return Order(
        idempotency_key=record.idempotency_key,
        symbol=record.symbol,
        side=record.side,
        qty=record.qty,
        order_type=record.order_type,
        urgency=record.urgency,
        ts=record.created_at,
        event_id=record.event_id,
        order_id=record.order_id,
        exit_rule=record.exit_rule,
    )

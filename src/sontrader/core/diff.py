"""목표 − 현재 → 주문 목록 (구현 계획 4단계). 순수 함수.

설계 2.4절의 핵심이 여기서 성립한다 — 전략은 **목표 상태**를 반환하고 주문은
이 모듈이 차이에서 유도한다. 그래서 중복 주문이 원리적으로 불가능하고, 재시작
복구가 자명하다 (같은 목표 + 같은 포지션 = 같은 주문).

## 여기서 처리하는 게이트 규칙 (설계 2.5절 표의 나머지)

| 규칙 | 구현 |
|---|---|
| no-trade band | 목표 비중과 현재 비중의 차가 임계치 미만이면 주문 생략 |
| 최소 주문금액 | 주문 금액이 하한 미만이면 생략 (소액 계좌 부스러기 방지) |

두 규칙 모두 **전량 청산에는 적용하지 않는다.** 노출을 없애는 주문을 금액이
작다는 이유로 생략하면 리스크가 남는다. 설계 1.3절의 집행 비대칭과 같은 논리다.

D+2 결제 제약과 가용 현금은 여기서 보지 않는다. `ctx.cash`가 있긴 하지만
매도 대금의 결제일 반영은 캘린더가 필요하고(구조 원칙 1), 현금 부족은 주문을
줄이는 게 아니라 미루는 문제라 엔진의 몫이다.

## 가격과 수량

수량 환산 기준은 `ctx.bars.latest(symbol).close` — **마지막 완성 봉의 종가**다.
NEXT_OPEN 진입의 실제 체결가는 다음 시가이므로 수량은 근사값이다. 목표 비중을
정확히 맞추는 것보다 look-ahead 없이 결정적으로 계산하는 쪽이 중요하다.

봉이 없으면 매수 수량을 정할 수 없어 주문을 만들지 않는다. **매도는 봉 없이도
나간다** — 수량이 포지션에 이미 있기 때문이다. 시세 조회 실패가 청산을 막는
경로를 남기지 않는다.

## 멱등 키

`{symbol}:{side}:{cycle_ts}` — 한 사이클 안에서 결정적이다. 네트워크 재시도나
사이클 도중 프로세스 재시작으로 같은 주문이 다시 만들어져도 키가 같아 중복
체결되지 않는다 (설계 2.6절).

사이클을 넘으면 키가 달라진다. 의도한 것이다 — 다음 사이클은 갱신된 포지션에서
차이를 다시 계산하므로 남은 만큼만 주문한다. 직전 사이클의 접수 불명(UNKNOWN)
주문은 키가 아니라 주문 조회로 해소한다 (`engine/reconcile.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sontrader.core.types import Context, Order, OrderType, Side, Target, Urgency


@dataclass(frozen=True)
class DiffConfig:
    """주문 생성 파라미터. 두 값 모두 설계 8절에 따라 백테스트로 확정한다."""

    # 목표 비중과 현재 비중의 차(비중 포인트). 0.02 = 자산의 2%.
    no_trade_band: float = 0.02
    min_order_value: int = 100_000  # 원

    def __post_init__(self) -> None:
        if not 0.0 <= self.no_trade_band < 1.0:
            raise ValueError(f"no_trade_band must be in [0, 1): {self.no_trade_band}")
        if self.min_order_value < 0:
            raise ValueError(f"min_order_value must be >= 0: {self.min_order_value}")


def to_orders(target: Target, ctx: Context, config: DiffConfig | None = None) -> list[Order]:
    """목표와 현재 포지션의 차이를 주문 목록으로 변환한다.

    청산 주문이 먼저 나온다. 긴급도가 이미 IMMEDIATE라 집행 순서가 보장되지만,
    목록 자체도 같은 순서로 두어야 로그와 승인 알림을 읽기 쉽다.
    """
    cfg = config or DiffConfig()

    exits: list[Order] = []
    rest: list[Order] = []

    # 1) 목표에서 빠졌거나 비중 0인 보유 종목 → 전량 청산.
    #    band·최소금액을 적용하지 않는다. 가격도 필요 없다.
    for pos in ctx.positions:
        item = target.get(pos.symbol)
        if item is not None and item.weight > 0.0:
            continue
        urgency = item.urgency if item is not None else Urgency.IMMEDIATE
        exits.append(
            _order(
                symbol=pos.symbol,
                side=Side.SELL,
                qty=pos.qty,
                urgency=urgency,
                now=ctx.now,
                event_id=pos.event_id,
            )
        )

    # 2) 비중이 남아 있는 항목 → 목표 수량과의 차이만큼 매수/매도.
    #    equity를 모르면 목표 수량이 전부 0이 되어 보유분을 통째로 팔아버린다.
    #    비중을 수량으로 환산할 수 없는 상황이므로 아무 주문도 만들지 않는다.
    if ctx.equity <= 0:
        return exits

    for item in target:
        if item.weight <= 0.0:
            continue
        bar = ctx.bars.latest(item.symbol)
        if bar is None or bar.close <= 0:
            # 가격을 모르면 수량을 정할 수 없다. 다음 사이클에 다시 시도한다.
            continue

        price = bar.close
        pos = ctx.position(item.symbol)
        current_qty = pos.qty if pos is not None else 0
        target_qty = int(ctx.equity * item.weight) // price
        delta = target_qty - current_qty
        if delta == 0:
            continue

        value = abs(delta) * price
        if value < cfg.min_order_value:
            continue
        if value / ctx.equity < cfg.no_trade_band:
            continue

        rest.append(
            _order(
                symbol=item.symbol,
                side=Side.BUY if delta > 0 else Side.SELL,
                qty=abs(delta),
                urgency=item.urgency,
                now=ctx.now,
                event_id=item.event_id,
            )
        )

    return exits + rest


def _order(
    *,
    symbol: str,
    side: Side,
    qty: int,
    urgency: Urgency,
    now: datetime,
    event_id: str | None,
) -> Order:
    # 진입도 청산도 시장가다. 차이는 타이밍(urgency)뿐이며, 그 해석은 집행기가
    # 한다 — IMMEDIATE는 장중 즉시, NEXT_OPEN은 다음 개장 시가 (설계 1.3절).
    return Order(
        idempotency_key=f"{symbol}:{side.value}:{now.isoformat()}",
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=OrderType.MARKET,
        urgency=urgency,
        ts=now,
        event_id=event_id,
    )

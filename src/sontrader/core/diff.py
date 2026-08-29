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

## 매매수량단위 (`ctx.trading_unit`)

KIS는 주문 수량이 `symbol_master.trading_unit`의 배수가 아니면 거부한다.
수량이 정해지는 곳이 여기 하나뿐이므로 여기서 맞춘다 — 어댑터에서 고치면
백테스트와 실전이 서로 다른 수량을 쓰게 되어 북극성이 깨진다.

**항상 내림한다.** 매수는 올림하면 가용 현금을 넘길 수 있고, 부분 매도는
올림하면 보유 수량을 넘겨 잔고 부족이 된다. 내림해서 0이 되면 주문을
만들지 않는다(다음 사이클에 자산이 늘면 다시 시도한다).

**전량 청산에는 적용하지 않는다.** 배수가 아니라는 이유로 노출을 남기면
안 되고, 단주(端株)는 실제로 매도가 가능하다 — band·최소금액을 청산에
적용하지 않는 것과 같은 논리다.

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

from sontrader.core.types import (
    Context,
    ExitReason,
    ExitRule,
    Order,
    OrderType,
    Side,
    Target,
    Urgency,
)


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


def floor_to_trading_unit(qty: int, unit: int) -> int:
    """`unit`의 배수로 내림한다. 매매수량단위 정책의 단일 출처.

    `adapters/broker_sim.py`도 이 함수를 쓴다 — 시뮬레이터가 현금 부족으로
    수량을 깎을 때 배수를 깨면, 백테스트만 체결되고 실전은 거부되는 주문이
    다시 생긴다.
    """
    if unit <= 1:
        return qty
    return qty - (qty % unit)


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
        if pos.symbol in ctx.pending_order_symbols:
            continue  # 이미 낸 청산 주문이 체결 대기 중 — 또 팔면 잔고 부족이다
        item = target.get(pos.symbol)
        if item is not None and item.weight > 0.0:
            continue
        urgency = item.urgency if item is not None else Urgency.IMMEDIATE
        # 청산은 봉이 없어도 나간다(위 참고). 기준가는 **있으면 싣고 없으면 만다** —
        # 사후 슬리피지 측정용일 뿐이라, 이것 때문에 청산이 막히면 안 된다.
        exit_bar = ctx.bars.latest(pos.symbol)
        exits.append(
            _order(
                symbol=pos.symbol,
                side=Side.SELL,
                qty=pos.qty,
                urgency=urgency,
                now=ctx.now,
                event_id=pos.event_id,
                # 전략이 판정한 청산 사유. 목표에서 그냥 빠진 종목(item is None)은
                # 사유가 없다 — 그건 청산 규칙이 아니라 리밸런싱이다.
                exit_reason=item.exit_reason if item is not None else None,
                ref_price=exit_bar.close if exit_bar is not None else None,
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
        if item.symbol in ctx.pending_order_symbols:
            # 이 종목으로 낸 주문이 아직 체결 대기 중이다. 브로커 잔고에는
            # 안 잡혀 있어 current_qty가 0으로 보이므로, 건너뛰지 않으면
            # 같은 종목을 한 번 더 산다 (`Context.pending_order_symbols` 참고).
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

        # 매매수량단위에 맞춰 내림한다. band·최소금액 판정보다 **먼저** 해야
        # 한다 — 통과시킨 뒤에 줄이면 실제로 나가는 주문이 두 하한을 밑돌 수
        # 있고, 그러면 두 게이트가 걸러내려던 부스러기 주문이 그대로 나간다.
        qty = floor_to_trading_unit(abs(delta), ctx.trading_unit(item.symbol))
        if qty == 0:
            continue

        value = qty * price
        if value < cfg.min_order_value:
            continue
        if value / ctx.equity < cfg.no_trade_band:
            continue

        side = Side.BUY if delta > 0 else Side.SELL
        rest.append(
            _order(
                symbol=item.symbol,
                side=side,
                qty=qty,
                urgency=item.urgency,
                now=ctx.now,
                event_id=item.event_id,
                # 매수만 청산 조건을 싣는다. 체결 시차 때문에 주문이 직접
                # 들고 가야 한다 — `core.types.Order` 참고.
                exit_rule=item.exit_rule if side is Side.BUY else None,
                # 수량을 환산한 바로 그 가격을 남긴다 — 사후 슬리피지 측정의
                # 기준가 (`apps/slippage.py`).
                ref_price=price,
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
    exit_rule: ExitRule | None = None,
    exit_reason: ExitReason | None = None,
    ref_price: int | None = None,
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
        exit_rule=exit_rule,
        exit_reason=exit_reason,
        ref_price=ref_price,
    )

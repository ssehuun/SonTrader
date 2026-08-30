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

D+2 결제 제약은 여기서 보지 않는다 — 매도 대금의 결제일 반영은 캘린더가
필요하다(구조 원칙 1).

## 🔴 증거금 — 매수 수량의 상한

**KIS는 시장가 매수 수량을 현재가 × 1.30으로 계산한다.** 현금이 9,447,178원이고
삼성전자가 257,000원이면 산수로는 36주지만 **시장가 주문은 28주가 상한**이다
(= 현금 ÷ 334,000). 넘겨서 내면 **부분 체결이 아니라 주문 전체가 거부된다**
(T17 — 실전이 그렇게 동작해서 백테스트도 맞췄다).

**이 상한을 안 지키면 체결 표본이 편향된다.** 예전에는 `현금 ÷ 종가`로 수량을
잡았는데, 다음 봉 시가가 조금이라도 높으면 거부되고 다음 사이클이 같은 계산을
반복했다. 결과: **진입 153건이 전부 갭하락일에 체결**됐다(기준선 47.4%).
미래를 엿본 것은 아니지만 **유리한 시가를 기다린 것과 같다.**

**가격제한폭이 곧 최대 갭이므로 이 상한을 지키면 거부가 원천적으로 불가능하다.**
같은 규칙이 제약이자 해법이다.

## 🔴 한 사이클 투입 상한은 76.9%다

한 사이클의 매수 주문은 **체결 전에 모두 접수**되므로 증거금 예약이 동시에 잡힌다.
n종목을 각각 비중 `w`로 사면:

    n × (w × 1.30) ≤ 1     →     총 투입 = n×w = 1/1.30 = 76.9%

**종목 수와 무관하다.** 4종목 × 20% = 80%도 넘는다. 그래서 기본값이
**4종목 × 19% = 76%**다 (`GateConfig.max_positions`, `StrategyConfig.entry_weight`).

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
    # 가격제한폭. 시장가 매수 수량의 상한을 정한다 — 아래 "증거금" 절 참고.
    # 2015-06-15부터 30%. 그 이전 구간을 재생할 때만 시대값으로 바꾼다.
    price_limit: float = 0.30

    def __post_init__(self) -> None:
        if not 0.0 <= self.no_trade_band < 1.0:
            raise ValueError(f"no_trade_band must be in [0, 1): {self.no_trade_band}")
        if self.min_order_value < 0:
            raise ValueError(f"min_order_value must be >= 0: {self.min_order_value}")
        if self.price_limit < 0.0:
            raise ValueError(f"price_limit must be >= 0: {self.price_limit}")


def _margin_unit_price(base_price: int, price_limit: float) -> int:
    """시장가 매수의 **가능수량계산단가** = 현재가 × (1 + 가격제한폭).

    KIS 매수가능조회(`inquire-psbl-order`, `v1_국내주식-007`)가
    `psbl_qty_calc_unpr`로 돌려주는 값이다. **공식 문서에 산식은 없다** —
    문서가 말하는 것은 *"00:지정가는 증거금율이 반영되지 않으므로, 증거금율이
    반영되는 01:시장가로 조회"* 까지다. 아래 배수는 실측이다.

    ## 실측 (2026-08-31, 읽기 전용 GET)

    | | 계산단가 | 기준 |
    |---|---|---|
    | **실전** 005930 (현재가 257,000) | **334,000** | 현재가 × 1.2996 |
    | **실전** 069500 (현재가 107,180) | **139,330** | 현재가 × 1.3000 |
    | 모의 005930 (기준가 266,000) | 345,500 | **전일 종가** × 1.30 |
    | 모의 069500 (기준가 109,135) | 141,875 | 전일 종가 × 1.30 |

    **실전과 모의의 기준 가격이 다르다.** 배수는 둘 다 1.30이고 호가단위 내림이
    들어간다(334,100 → 334,000). **실전 규칙을 쓴다** — 주문 시점의 마지막 완성
    봉 종가가 곧 "현재가"이므로 `bar.close`가 그 값이다.

    수량 산식 `floor(주문가능현금 ÷ 계산단가)`는 **모의에서만 확인했다**
    (`nrcvb_buy_qty` 27주 / 66주와 정확히 일치). ⚠️ **실전은 계좌 현금이
    1,070원뿐이라 검증하지 못했다.**

    ## 왜 이 상한이 편향까지 없애나

    **가격제한폭이 곧 하루 최대 갭이다.** 다음 봉 시가는 이 값을 넘을 수 없으므로
    **현금 부족 거부가 원리적으로 불가능**해진다. 같은 규칙이 제약이자 보증이다.

    ## 넣지 않은 것

    **호가단위 내림.** 실제 계산단가는 틱 내림이 들어가 이 값보다 조금 작으므로
    여기 값은 **약간 보수적**이다(수량이 1주 덜 나올 수 있다). 남는 현금은 다음
    사이클이 채운다. 대신 **지수처럼 호가단위가 없는 계열에도 같은 코드가 쓰인다.**

    **거래비용.** 시가가 정확히 상한가인 날(상한가 시초가)에는 비용만큼 모자라
    거부될 수 있다. `+1`원 여유뿐이다. 드물고 다음 사이클이 처리하므로 손잡이를
    늘리지 않았다.

    ⚠️ 실전 증거금률이 다르면 **이 함수 하나만** 고치면 된다.
    """
    return int(base_price * (1.0 + price_limit)) + 1


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

    # 한 사이클의 매수 주문들은 **체결 전에 모두 접수**되므로 증거금 예약이
    # 동시에 잡힌다. 종목마다 `ctx.cash`를 그대로 보면 n개가 각각 통과해
    # 합계가 현금을 넘는다 — 잔여를 들고 차감한다.
    remaining_cash = ctx.cash

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

        # 매수는 KIS가 받아주는 수량을 넘지 않는다 (아래 "증거금" 절).
        # 넘겨서 내면 주문이 **통째로** 거부되고, 다음 사이클에 같은 계산을
        # 반복하므로 값이 내린 날에만 체결된다 — 체결 표본이 편향된다.
        capped = False
        margin_unit = _margin_unit_price(price, cfg.price_limit)
        if delta > 0:
            affordable = remaining_cash // margin_unit
            if affordable <= 0:
                continue
            if affordable < delta:
                delta = affordable
                capped = True

        # 매매수량단위에 맞춰 내림한다. band·최소금액 판정보다 **먼저** 해야
        # 한다 — 통과시킨 뒤에 줄이면 실제로 나가는 주문이 두 하한을 밑돌 수
        # 있고, 그러면 두 게이트가 걸러내려던 부스러기 주문이 그대로 나간다.
        qty = floor_to_trading_unit(abs(delta), ctx.trading_unit(item.symbol))
        if qty == 0:
            continue

        value = qty * price
        # 증거금에 걸려 줄인 주문에는 두 하한을 적용하지 않는다. 이 둘은
        # **가격이 움직여 생긴 부스러기**를 막는 장치이지, 사려다 못 산 잔량을
        # 막는 장치가 아니다. 적용하면 마지막 몇 %가 영구히 안 채워진다.
        if not capped:
            if value < cfg.min_order_value:
                continue
            if value / ctx.equity < cfg.no_trade_band:
                continue

        side = Side.BUY if delta > 0 else Side.SELL
        if side is Side.BUY:
            # 체결가가 아니라 **증거금 단가**로 차감한다. 접수 시점에
            # 잡히는 금액이 그것이고, 체결 후 차액은 풀린다.
            remaining_cash -= qty * margin_unit
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

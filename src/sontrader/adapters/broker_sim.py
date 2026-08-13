"""시뮬레이션 체결 (구현 계획 5단계). 02문서 §2: "broker_sim.py — 시뮬레이션
체결 (슬리피지·거래세·D+2)".

## 체결가는 항상 "다음 봉의 시가"다

01문서 §4.1과 05문서 §4.1이 명시적으로 확정한 것: "백테스트에서 손절은
반드시 시가 체결로 시뮬레이션한다" + "진입은 다음 개장 시가, 손절은 시가
체결". `urgency`(IMMEDIATE/NEXT_OPEN)와 무관하게 **모든 주문은 그 주문이
만들어진 사이클(`order.ts`) 다음 봉의 시가에 체결된다.** 신호가 발생한 봉의
종가로 즉시 체결하면(레거시 `backtest_manager.py`의 버그, 05문서 §4.1) 그
종가를 보고 판단한 뒤 같은 종가에 체결하는 셈이라 look-ahead다.

그래서 이 클래스는 `Context.bars`(현재 시각까지만 보이는 제한된 뷰)가 아니라
**미래를 포함한 전체 시계열**을 생성자에서 따로 받는다. 시뮬레이터는 "시장"
역할이므로 전체 데이터를 알아도 되고, 오히려 알아야 한다 — 실전에서 내일
시가가 얼마일지 결정하는 건 거래소지 트레이더가 아니다. `Context.bars`가
막아야 하는 대상은 전략(strategy/gate/diff)이지 체결 시뮬레이터가 아니다.

## 현금·정산

- **매수**는 즉시 현금을 차감한다. 살 수 있는 현금(`cash()`, 정산 완료분만)을
  넘는 매수는 미수를 만들지 않도록 살 수 있는 수량만큼만 체결한다(그마저
  0이면 거부). 설계 2.6절의 "결제 제약: D+2. 매도 대금 즉시 재사용 시 미수
  발생 소지"를 실제로 막는 지점이 여기다.
- **매도**는 보유 수량을 즉시 줄이지만(다음 사이클 전략이 최신 보유 상태를
  봐야 하므로), 대금은 바로 쓸 수 있는 현금이 되지 않는다 — D+settlement_days
  뒤에 정산 대기열에서 풀려나온다. `submit()`을 호출할 때마다 그 시각까지
  만기된 정산을 먼저 반영한다.
- 정산일은 **캘린더일**로 근사한다. 거래일 캘린더 소스는 01문서 §8의 미확정
  파라미터라 아직 없다 — 실제 거래일 캘린더가 정해지면 이 근사를 교체해야
  한다(휴장일이 껴 있으면 실제보다 정산이 하루이틀 빨리 잡힐 수 있다).

## 다루지 않는 것

- **지정가 주문.** `core/diff.py`는 시장가만 만든다(진입도 청산도 시장가 —
  타이밍만 urgency로 구분). 지정가가 들어오면 조용히 잘못 처리하느니
  `NotImplementedError`로 막는다.
- **부분체결(유동성 부족으로 인한).** 우리 쪽 현금 부족으로 인한 부분체결만
  다룬다. 거래소 유동성으로 인한 부분체결은 `broker_kis.py`(7단계)의
  관심사다 — 시뮬레이션에서는 다음 봉 시가에 원하는 수량 전부를 체결할 수
  있다고 가정한다.
"""

from __future__ import annotations

import bisect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sontrader.adapters.broker import BrokerPosition, OrderResult
from sontrader.core.types import Bar, Fill, Order, OrderStatus, OrderType, Side


@dataclass(frozen=True)
class SimBrokerConfig:
    """거래비용 파라미터. 전부 01문서 §8 "미확정 파라미터"에 해당한다 — 여기
    기본값은 자리표시자이며, 실제 백테스트 결과를 신뢰하려면 확정된 요율로
    교체해야 한다."""

    commission_rate: float = 0.00015  # 매수·매도 공통 위탁수수료 (플레이스홀더)
    tax_rate: float = 0.0018  # 매도 시 증권거래세 등 (플레이스홀더, 시장·시점별로 다름)
    slippage_bps: float = 10.0  # 시가 기준. 매수는 불리하게 위로, 매도는 아래로
    settlement_days: int = 2  # D+2. 캘린더일 근사 (위 docstring 참고)

    def __post_init__(self) -> None:
        if self.commission_rate < 0:
            raise ValueError(f"commission_rate must be >= 0: {self.commission_rate}")
        if self.tax_rate < 0:
            raise ValueError(f"tax_rate must be >= 0: {self.tax_rate}")
        if self.slippage_bps < 0:
            raise ValueError(f"slippage_bps must be >= 0: {self.slippage_bps}")
        if self.settlement_days < 0:
            raise ValueError(f"settlement_days must be >= 0: {self.settlement_days}")


class SimBroker:
    def __init__(
        self,
        bars: Mapping[str, Sequence[Bar]],
        *,
        initial_cash: int,
        config: SimBrokerConfig | None = None,
    ) -> None:
        if initial_cash < 0:
            raise ValueError(f"initial_cash must be >= 0: {initial_cash}")
        self._bars: dict[str, list[Bar]] = {
            symbol: sorted(rows, key=lambda b: b.ts) for symbol, rows in bars.items()
        }
        self._timestamps: dict[str, list[datetime]] = {
            symbol: [bar.ts for bar in rows] for symbol, rows in self._bars.items()
        }
        self._config = config or SimBrokerConfig()
        self._cash = initial_cash
        self._positions: dict[str, BrokerPosition] = {}
        self._pending: list[tuple[date, int]] = []  # (정산일, 입금액)
        self._total_costs = 0  # 누적 수수료 + 증권거래세 (report.py의 "총 거래비용 비중")

    def submit(self, orders: list[Order], *, now: datetime) -> list[OrderResult]:
        self._settle(now.date())
        return [self._fill_one(order) for order in orders]

    def positions(self) -> list[BrokerPosition]:
        return list(self._positions.values())

    def cash(self) -> int:
        return self._cash

    @property
    def pending_settlement(self) -> int:
        """D+정산 대기 중인 금액 합계 — 테스트·디버깅용 조회."""
        return sum(amount for _, amount in self._pending)

    @property
    def total_costs(self) -> int:
        """지금까지 낸 수수료 + 증권거래세 합계 (슬리피지는 제외 — 그건 체결가에
        이미 녹아 있어 "비용"과 "가격" 경계가 다르다). `apps/report.py`의 총
        거래비용 비중 계산 입력."""
        return self._total_costs

    # --- 내부 ---------------------------------------------------------------

    def _settle(self, as_of: date) -> None:
        matured = [amount for due, amount in self._pending if due <= as_of]
        if not matured:
            return
        self._cash += sum(matured)
        self._pending = [(due, amount) for due, amount in self._pending if due > as_of]

    def _fill_one(self, order: Order) -> OrderResult:
        if order.order_type is OrderType.LIMIT:
            raise NotImplementedError("SimBroker only fills market orders")

        next_bar = self._next_bar(order.symbol, order.ts)
        if next_bar is None:
            # 이 시점 이후 봉이 없다 — 백테스트 데이터의 끝. 실전의 "접수
            # 불명"과 다루는 이유는 다르지만(API 타임아웃이 아니라 데이터
            # 부재), 체결됐는지 알 방법이 없다는 결과는 같다.
            return OrderResult(order=order, status=OrderStatus.UNKNOWN)

        if order.side is Side.BUY:
            return self._fill_buy(order, next_bar)
        return self._fill_sell(order, next_bar)

    def _next_bar(self, symbol: str, after: datetime) -> Bar | None:
        timestamps = self._timestamps.get(symbol, [])
        idx = bisect.bisect_right(timestamps, after)
        bars = self._bars.get(symbol, [])
        return bars[idx] if idx < len(bars) else None

    def _fill_buy(self, order: Order, bar: Bar) -> OrderResult:
        price = bar.open * (1.0 + self._config.slippage_bps / 10_000)
        qty = order.qty
        total, commission = _order_cost(price, qty, self._config.commission_rate)

        if total > self._cash:
            # 미수를 만들지 않는다 — 살 수 있는 만큼만 산다.
            qty = int(self._cash / (price * (1.0 + self._config.commission_rate)))
            if qty <= 0:
                return OrderResult(order=order, status=OrderStatus.REJECTED)
            total, commission = _order_cost(price, qty, self._config.commission_rate)

        self._cash -= total
        self._total_costs += commission
        self._add_position(order.symbol, qty, price)
        status = OrderStatus.FILLED if qty == order.qty else OrderStatus.PARTIAL
        fill = _fill(order, price, qty, bar.ts)
        return OrderResult(order=order, status=status, fills=(fill,))

    def _fill_sell(self, order: Order, bar: Bar) -> OrderResult:
        held = self._positions.get(order.symbol)
        if held is None or held.qty < order.qty:
            # core/diff.py는 항상 보유 수량 이하로만 매도 주문을 만든다. 이걸
            # 넘어서면 어딘가에서 상태가 어긋난 것이므로 조용히 넘기지 않는다.
            raise ValueError(
                f"sell qty {order.qty} exceeds held qty "
                f"{held.qty if held else 0} for {order.symbol!r}"
            )

        price = bar.open * (1.0 - self._config.slippage_bps / 10_000)
        gross = _round(price * order.qty)
        commission = _round(gross * self._config.commission_rate)
        tax = _round(gross * self._config.tax_rate)
        proceeds = gross - commission - tax

        self._total_costs += commission + tax
        self._remove_position(order.symbol, order.qty)
        settle_date = bar.ts.date() + timedelta(days=self._config.settlement_days)
        self._pending.append((settle_date, proceeds))

        fill = _fill(order, price, order.qty, bar.ts)
        return OrderResult(order=order, status=OrderStatus.FILLED, fills=(fill,))

    def _add_position(self, symbol: str, qty: int, price: float) -> None:
        existing = self._positions.get(symbol)
        if existing is None:
            self._positions[symbol] = BrokerPosition(symbol=symbol, qty=qty, avg_price=price)
            return
        total_qty = existing.qty + qty
        avg_price = (existing.avg_price * existing.qty + price * qty) / total_qty
        self._positions[symbol] = BrokerPosition(symbol=symbol, qty=total_qty, avg_price=avg_price)

    def _remove_position(self, symbol: str, qty: int) -> None:
        existing = self._positions[symbol]
        remaining = existing.qty - qty
        if remaining == 0:
            del self._positions[symbol]
        else:
            self._positions[symbol] = BrokerPosition(
                symbol=symbol, qty=remaining, avg_price=existing.avg_price
            )


def _order_cost(price: float, qty: int, commission_rate: float) -> tuple[int, int]:
    """(총 지불액, 수수료) — 호출자가 수수료를 누적 비용에 더로 쓴다."""
    cost = _round(price * qty)
    commission = _round(cost * commission_rate)
    return cost + commission, commission


def _fill(order: Order, price: float, qty: int, ts: datetime) -> Fill:
    return Fill(
        order_id=order.order_id or order.idempotency_key, price=_round(price), qty=qty, ts=ts
    )


def _round(value: float) -> int:
    return int(round(value))

"""시뮬레이션 체결 (구현 계획 5단계). 02문서 §2: "broker_sim.py — 시뮬레이션
체결 (슬리피지·거래세·D+2)".

## 체결 시점은 둘이다 — 기본은 "다음 봉의 시가"

01문서 §4.1과 05문서 §4.1이 명시적으로 확정한 것: "백테스트에서 손절은
반드시 시가 체결로 시뮬레이션한다" + "진입은 다음 개장 시가, 손절은 시가
체결". `urgency`(IMMEDIATE/NEXT_OPEN)와 무관하게 **모든 주문은 그 주문이
만들어진 사이클(`order.ts`) 다음 봉의 시가에 체결된다.**

그래서 이 클래스는 `Context.bars`(현재 시각까지만 보이는 제한된 뷰)가 아니라
**미래를 포함한 전체 시계열**을 생성자에서 따로 받는다. 시뮬레이터는 "시장"
역할이므로 전체 데이터를 알아도 되고, 오히려 알아야 한다 — 실전에서 내일
시가가 얼마일지 결정하는 건 거래소지 트레이더가 아니다. `Context.bars`가
막아야 하는 대상은 전략(strategy/gate/diff)이지 체결 시뮬레이터가 아니다.

## `fill_at_close` — 15:20 종가 동시호가 집행

`fill_at_close=True`면 **신호가 난 그 봉의 종가**에 체결한다. 15:20 종가
동시호가에 주문을 넣는 집행을 재생하는 모드다.

**왜 필요한가**: 시가 체결은 체결가를 주문 시점에 모른다. 그래서 수량을
증거금 상한(현재가 × 1.30)으로 잡아야 하고, 거기서 분할 체결·미투입 현금·
진입 첫날 노출 부족이 줄줄이 딸려 나온다. 그 배관이 전략 효과보다 큰
편차를 만들었다(`docs/research/03-측정.md` M007·M008). 종가 체결은 체결가를
주문 시점에 알므로 그 배관이 통째로 사라진다.

⚠️ **대가: 신호와 체결이 같은 가격을 쓴다.** 신호는 `종가 > SMA(N)`이고
체결도 그 종가다 — 실전에서는 15:20에 주문할 때 오늘 종가가 아직 확정되지
않았으므로, **일봉으로는 15:20 가격을 종가로 대용**하는 근사다. 방향이 한쪽
(유리한 쪽)이라 이 모드로 잰 수치는 **실전보다 낙관**이다. 판정문에 그대로
옮긴다.

**기본값은 여전히 `False`다.** 종목 전략·기준선은 시가 체결로 잰 값이고,
기본을 바꾸면 그 수치들이 조용히 움직인다.

## 현금·정산

- **매수**는 즉시 현금을 차감한다. 살 수 있는 현금(`cash()`, 정산 완료분만)을
  넘는 매수는 **주문 전체를 거부한다.** 설계 2.6절의 "결제 제약: D+2. 매도
  대금 즉시 재사용 시 미수 발생 소지"를 실제로 막는 지점이 여기다.

  **왜 깎지 않고 거부하나** — 2026-08-26까지는 살 수 있는 만큼만 깎아서
  체결했다. 두 가지 이유로 틀렸다:

  1. **실전 KIS가 그렇게 동작하지 않는다.** 주문가능금액이 모자라면 수량을
     줄여 받아주는 것이 아니라 **주문 전체를 거부**한다. 깎아서 체결시키면
     실전에서 존재할 수 없는 포지션이 백테스트 성과에 잡힌다. 실측으로
     매수 주문 1,466건 중 **435건(29.7%)이 이 경로로 체결**되고 있었다 —
     이미 거부되던 471건까지 합치면 **61.8%가 실전이라면 안 나갔을 주문**이다.
  2. **설계가 이미 그렇게 정해 뒀다.** `core/diff.py` 상단: *"현금 부족은
     주문을 줄이는 게 아니라 **미루는** 문제"*. 미루는 것은 저절로 된다 —
     다음 사이클에 diff가 같은 차이를 다시 계산해 주문을 재생성하고,
     그때는 D+2 정산이 풀려 현금이 있을 수 있다.

  **여전히 낙관적이다.** KIS는 시장가 매수의 필요 증거금을 상한가 기준으로
  잡는 등 여기보다 **더 엄격**할 수 있다. 그 공식을 추측해 넣지는 않았다.
- **매도**는 보유 수량을 즉시 줄이지만(다음 사이클 전략이 최신 보유 상태를
  봐야 하므로), 대금은 바로 쓸 수 있는 현금이 되지 않는다 — D+settlement_days
  뒤에 정산 대기열에서 풀려나온다. `submit()`을 호출할 때마다 그 시각까지
  만기된 정산을 먼저 반영한다.
- 정산일은 **캘린더일**로 근사한다. 거래일 캘린더 소스는 01문서 §8의 미확정
  파라미터라 아직 없다 — 실제 거래일 캘린더가 정해지면 이 근사를 교체해야
  한다(휴장일이 껴 있으면 실제보다 정산이 하루이틀 빨리 잡힐 수 있다).

## 거래정지일에는 체결하지 않는다

KIS 일별시세는 거래정지일에도 봉을 준다 — **거래량 0, OHLC는 전부 직전
종가**. 그 시가에 체결시키면 현실에서 불가능한 거래가 성과로 잡히고, 정지 중
스톱이 발동하면 하락이 반영되기 전 가격에 팔려 손실이 과소평가된다. 그래서
`_next_bar()`가 거래량 0인 봉을 건너뛰고 재개 봉을 찾는다 — 정지 중에 낸
시장가 주문이 재개 시점에 집행되는 현실과 같다.

진입 쪽은 `data/universe.py`의 거래정지 필터가 먼저 막지만, 이미 보유 중인
종목이 정지되는 경우는 여기서만 처리할 수 있다.

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
    """거래비용 파라미터. 01문서 §8 "미확정 파라미터" 중 수수료·거래세는
    한국투자증권 실제 계좌 기준으로 확정했다(2026-08 확인) — `slippage_bps`와
    `settlement_days`(캘린더일 근사)는 여전히 미확정이라 자리표시자로 남는다."""

    # 매수·매도 공통 위탁수수료. KRX 기준(0.0140527%) — 이 시스템은 NXT로
    # 주문을 라우팅하지 않는다(client.py가 시세·주문 전부 FID_COND_MRKT_DIV_CODE
    # "J"=KRX만 쓴다). NXT 매매수수료는 0.0130527%로 더 낮지만, 주문 라우팅을
    # 실제로 구현하기 전까지는 적용 대상이 없다.
    commission_rate: float = 0.0140527 / 100
    tax_rate: float = 0.002  # 매도 시 증권거래세, 코스피·코스닥 공통 0.2%(농특세 등 포함)
    slippage_bps: float = 10.0  # 체결 기준가. 매수는 불리하게 위로, 매도는 아래로
    settlement_days: int = 2  # D+2. 캘린더일 근사 (위 docstring 참고)
    # True면 신호 봉의 **종가**에 체결한다 (15:20 종가 동시호가 집행).
    # 위 docstring의 `fill_at_close` 절 — 근사와 그 대가가 거기 있다.
    fill_at_close: bool = False

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
        # 매매수량단위는 여기서 보지 않는다 — 수량을 정하는 책임은 전적으로
        # `core/diff.py`에 있고(실전과 백테스트가 같은 코드로 정해야 한다),
        # 이 클래스는 더 이상 수량을 깎지 않는다.
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

        bar = self._fill_bar(order.symbol, order.ts)
        if bar is None:
            # 이 시점 이후 봉이 없다 — 백테스트 데이터의 끝. 실전의 "접수
            # 불명"과 다루는 이유는 다르지만(API 타임아웃이 아니라 데이터
            # 부재), 체결됐는지 알 방법이 없다는 결과는 같다.
            return OrderResult(order=order, status=OrderStatus.UNKNOWN)

        if order.side is Side.BUY:
            return self._fill_buy(order, bar)
        return self._fill_sell(order, bar)

    def _fill_bar(self, symbol: str, order_ts: datetime) -> Bar | None:
        """체결이 일어나는 봉. 모드에 따라 신호 봉 자신이거나 다음 봉이다."""
        if self._config.fill_at_close:
            return self._bar_at_or_after(symbol, order_ts)
        return self._next_bar(symbol, order_ts)

    def _fill_price(self, bar: Bar, *, sign: float) -> float:
        """`sign`은 매수 +1 / 매도 −1 — 슬리피지가 항상 불리한 쪽으로 간다."""
        base = bar.close if self._config.fill_at_close else bar.open
        return base * (1.0 + sign * self._config.slippage_bps / 10_000)

    def _bar_at_or_after(self, symbol: str, ts: datetime) -> Bar | None:
        """`ts` **이상**인 첫 거래 가능한 봉 — 종가 체결 모드의 대상 봉.

        사이클 시각이 그날 봉의 `ts`와 같으므로(일봉은 둘 다 00:00) 보통은
        그날 봉 자신이다. 거래정지(거래량 0)면 `_next_bar()`와 같은 이유로
        재개 봉까지 밀린다.
        """
        timestamps = self._timestamps.get(symbol, [])
        bars = self._bars.get(symbol, [])
        idx = bisect.bisect_left(timestamps, ts)
        while idx < len(bars):
            if bars[idx].volume > 0:
                return bars[idx]
            idx += 1
        return None

    def _next_bar(self, symbol: str, after: datetime) -> Bar | None:
        """`after` 이후 첫 **거래 가능한** 봉.

        거래량 0인 봉은 건너뛴다. KIS 일별시세는 거래정지일에도 봉을 주는데
        (거래량 0, OHLC는 전부 직전 종가) 그 시가에 체결시키면 현실에서
        불가능한 거래가 성과로 잡힌다. 실측으로 전체 봉의 3.2%가 이런 봉이고,
        2,463종목 중 433종목에 정지 이력이 있다(평균 61일, 최대 334일).

        건너뛴 결과는 "재개일 시가에 체결"이 되는데, 이게 현실과 맞다 — 정지
        중에 낸 시장가 주문은 재개 시점에 집행된다. 정지 중 스톱이 발동해도
        정지 직전 가격이 아니라 재개가로 팔린다는 뜻이고, 그래서 백테스트가
        손실을 과소평가하지 않는다.

        재개 봉이 끝내 없으면(상장폐지·데이터 끝) None → 호출자가 UNKNOWN.
        """
        timestamps = self._timestamps.get(symbol, [])
        bars = self._bars.get(symbol, [])
        idx = bisect.bisect_right(timestamps, after)
        while idx < len(bars):
            if bars[idx].volume > 0:
                return bars[idx]
            idx += 1
        return None

    def _fill_buy(self, order: Order, bar: Bar) -> OrderResult:
        price = self._fill_price(bar, sign=+1.0)
        total, commission = _order_cost(price, order.qty, self._config.commission_rate)

        if total > self._cash:
            # 현금이 모자라면 **주문 전체를 거부**한다 — 실전 KIS가 그렇게
            # 하고, 설계도 "줄이지 말고 미루라"고 정해 뒀다(위 docstring).
            # 다음 사이클에 diff가 같은 주문을 다시 만들며, 그때는 D+2 정산이
            # 풀려 현금이 있을 수 있다.
            return OrderResult(order=order, status=OrderStatus.REJECTED)

        self._cash -= total
        self._total_costs += commission
        self._add_position(order.symbol, order.qty, price)
        # 현금 부족은 이제 거부이고 유동성 부족은 모델링하지 않으므로, 매수
        # 체결은 전량 아니면 없음이다. PARTIAL이 나올 경로가 남아 있지 않다.
        fill = _fill(order, price, order.qty, bar.ts)
        return OrderResult(order=order, status=OrderStatus.FILLED, fills=(fill,))

    def _fill_sell(self, order: Order, bar: Bar) -> OrderResult:
        held = self._positions.get(order.symbol)
        if held is None or held.qty < order.qty:
            # core/diff.py는 항상 보유 수량 이하로만 매도 주문을 만든다. 이걸
            # 넘어서면 어딘가에서 상태가 어긋난 것이므로 조용히 넘기지 않는다.
            raise ValueError(
                f"sell qty {order.qty} exceeds held qty "
                f"{held.qty if held else 0} for {order.symbol!r}"
            )

        price = self._fill_price(bar, sign=-1.0)
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

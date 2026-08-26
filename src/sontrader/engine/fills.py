"""체결 → 포지션 상태 변경 규칙 (공용).

## 왜 별도 모듈인가

같은 규칙이 두 곳에서 필요하다. 백테스트는 결과를 메모리 dict에 쌓고, 실전은
`positions` 테이블에 쓴다. **저장소만 다르고 규칙은 같다.**

규칙을 양쪽에 복제하면 언젠가 갈라지고, 갈라진 순간 "백테스트로 검증한 전략을
실전이 그대로 돌린다"는 이 시스템의 전제가 무너진다. 그래서 판단만 여기서
하고 저장은 호출자에게 맡긴다.

## 옮겨 온 규칙 셋

전부 백테스트가 이미 지키던 것이다.

1. **이미 보유 중인 종목의 추가 체결은 진입 정보를 갈아끼우지 않는다.**
   "진입 시 확정, 보유 중 불변"(설계 3.1절). 드리프트 리밸런싱으로 조금 더
   샀다고 진입시각·진입가·청산조건이 바뀌면 스톱 레벨이 통째로 움직인다.
2. **보유 잔량이 0이 될 때만 포지션을 닫는다.**
3. **청산 시각을 남긴다.** 게이트의 쿨다운 판정 근거다.

## 규칙 2는 "주문"이 아니라 "포지션"을 본다 — 예전엔 아니었다

2026-08-26까지 이 판정은 `order.qty - filled > 0`, 즉 **주문이 전량
체결됐는가**였다. 그런데 드리프트 리밸런싱 트림(18주 보유 → 9주 매도)은
*주문 기준으로는 전량체결*이라 `Closed`가 나갔다. 결과:

- 브로커에는 9주가 남았는데 호출자가 진입 메타데이터를 지웠다 →
  **전략에게 보이지 않는 고아 포지션**. 스톱도 최대보유일도 영영 안 걸린다.
- `ctx.positions`에 없으니 게이트의 `max_positions`가 고아를 안 센다 →
  슬롯이 비어 보여 신규 진입이 계속 들어갔다. 실측으로 **전체 사이클의
  94.9%에서 상한 5종목이 깨져 있었다**(최대 11종목).
- 트림이 "청산"으로 성과 지표에 섞였다. 878건 중 121건(13.8%)이 트림이고,
  트림은 **이익 난 포지션을 깎은 것만 모인 표본**이라(승률 58.7%) 지표를
  체계적으로 위로 편향시켰다.

그래서 `held`가 종목 집합이 아니라 **종목 → 수량**이다. 잔량을 모르면
"포지션이 비었는가"를 판정할 수 없고, 판정할 수 없으면 이 버그가 되돌아온다.

## 청산 조건은 주문이 들고 온다

`order.exit_rule`을 쓴다. 백테스트는 예전에 `CycleResult.target`에서 꺼냈지만
그 방식은 실전에서 성립하지 않는다 — 08:35에 낸 주문이 09:00에 체결되면,
체결을 확인하는 사이클의 목표에는 그 종목이 없을 수 있다(`core.types.Order`
참고). 양쪽이 같은 출처를 쓰도록 주문 쪽으로 통일했다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sontrader.adapters.broker import OrderResult
from sontrader.core.types import ExitRule, OrderStatus, Side


@dataclass(frozen=True)
class Opened:
    """새 포지션이 생겼다 (기존 보유가 없던 종목의 매수 체결)."""

    symbol: str
    entered_at: datetime
    entry_price: int
    qty: int
    exit_rule: ExitRule
    event_id: str | None


@dataclass(frozen=True)
class Closed:
    """포지션이 전량 청산됐다."""

    symbol: str
    exited_at: datetime
    exit_price: int
    qty: int


def position_changes(
    order_results: list[OrderResult], *, held: Mapping[str, int]
) -> list[Opened | Closed]:
    """체결 결과 → 포지션 변경 목록. 순수 함수 — 저장하지 않는다.

    `held`는 이번 체결을 반영하기 **전**의 보유 종목과 수량이다. 규칙 1(진입
    정보 불변)과 규칙 2(잔량 0일 때만 청산)를 둘 다 여기서 판정한다.

    수량을 사이클 안에서 누적해 간다 — 한 사이클에 같은 종목으로 여러 체결이
    들어와도(부분체결 분할, 추가 매수 뒤 매도) 잔량이 일관되게 계산돼야
    하기 때문이다.
    """
    remaining = dict(held)
    changes: list[Opened | Closed] = []
    opened: set[str] = set()

    for result in order_results:
        if result.status not in (OrderStatus.FILLED, OrderStatus.PARTIAL):
            continue
        if not result.fills:
            continue
        order = result.order
        fill = result.fills[0]
        filled = sum(f.qty for f in result.fills)

        if order.side is Side.BUY:
            if order.symbol in remaining or order.symbol in opened:
                # 규칙 1 — 추가 체결은 진입 정보를 건드리지 않는다.
                # 다만 잔량에는 반영해야 한다: 반영하지 않으면 뒤따르는 매도가
                # 실제보다 적은 보유를 기준으로 판정돼 조기에 청산으로 잡힌다.
                remaining[order.symbol] = remaining.get(order.symbol, 0) + filled
                continue
            opened.add(order.symbol)
            remaining[order.symbol] = filled
            changes.append(
                Opened(
                    symbol=order.symbol,
                    entered_at=fill.ts,
                    entry_price=fill.price,
                    qty=filled,
                    # 주문이 청산 조건을 안 들고 왔으면 기본값으로 붙인다 —
                    # 조건 없는 포지션은 스톱이 영영 발동하지 않는다(fail-closed).
                    exit_rule=order.exit_rule or ExitRule(),
                    event_id=order.event_id,
                )
            )
        else:
            # 규칙 2 — **주문이** 전량 체결됐는지가 아니라 **포지션이** 비었는지를
            # 본다. 트림(보유 18주 중 9주 매도)은 주문 기준으로는 전량체결이라
            # 예전 판정은 이걸 청산으로 잡았고, 그래서 고아 포지션이 생겼다
            # (위 docstring).
            after = remaining.get(order.symbol, 0) - filled
            remaining[order.symbol] = after
            if after > 0:
                continue
            changes.append(
                Closed(
                    symbol=order.symbol,
                    exited_at=fill.ts,
                    exit_price=fill.price,
                    qty=filled,
                )
            )

    return changes

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
2. **전량 체결일 때만 포지션을 닫는다.** 부분 매도는 보유를 유지한다.
3. **청산 시각을 남긴다.** 게이트의 쿨다운 판정 근거다.

## 청산 조건은 주문이 들고 온다

`order.exit_rule`을 쓴다. 백테스트는 예전에 `CycleResult.target`에서 꺼냈지만
그 방식은 실전에서 성립하지 않는다 — 08:35에 낸 주문이 09:00에 체결되면,
체결을 확인하는 사이클의 목표에는 그 종목이 없을 수 있다(`core.types.Order`
참고). 양쪽이 같은 출처를 쓰도록 주문 쪽으로 통일했다.
"""

from __future__ import annotations

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
    order_results: list[OrderResult], *, held: frozenset[str]
) -> list[Opened | Closed]:
    """체결 결과 → 포지션 변경 목록. 순수 함수 — 저장하지 않는다.

    `held`는 이번 체결을 반영하기 **전**의 보유 종목이다. 규칙 1(진입 정보
    불변)을 판정하는 데 쓴다.
    """
    changes: list[Opened | Closed] = []
    opened: set[str] = set()

    for result in order_results:
        if result.status not in (OrderStatus.FILLED, OrderStatus.PARTIAL):
            continue
        if not result.fills:
            continue
        order = result.order
        fill = result.fills[0]

        if order.side is Side.BUY:
            if order.symbol in held or order.symbol in opened:
                continue  # 규칙 1 — 추가 체결은 진입 정보를 건드리지 않는다
            opened.add(order.symbol)
            changes.append(
                Opened(
                    symbol=order.symbol,
                    entered_at=fill.ts,
                    entry_price=fill.price,
                    qty=sum(f.qty for f in result.fills),
                    # 주문이 청산 조건을 안 들고 왔으면 기본값으로 붙인다 —
                    # 조건 없는 포지션은 스톱이 영영 발동하지 않는다(fail-closed).
                    exit_rule=order.exit_rule or ExitRule(),
                    event_id=order.event_id,
                )
            )
        else:
            filled = sum(f.qty for f in result.fills)
            if order.qty - filled > 0:
                continue  # 규칙 2 — 부분 매도는 보유를 유지한다
            changes.append(
                Closed(
                    symbol=order.symbol,
                    exited_at=fill.ts,
                    exit_price=fill.price,
                    qty=filled,
                )
            )

    return changes

"""재시작 시 상태 재구성 (구현 계획 5단계 잔여 작업). 01문서 §6.4-6.5.

## 왜 필요한가

"상태는 복구하는 것이 아니라 재구성하는 것이다"(01문서 §1.1). 장중 루프가
죽으면 손절이 발동하지 않으므로(01문서 §6.4), 재시작 직후 아무 전제 없이
바로 매매를 재개하면 안 된다 — 그 사이 접수 여부가 불명확해진 주문이 있을
수 있고, 우리 DB가 아는 포지션과 KIS 계좌의 실제 잔고가 어긋나 있을 수 있다.

## 처리 순서

1. **미체결 주문 해소** (`KisBroker.resolve_unknown()`, 7단계) — 크래시 시점에
   접수 여부가 불명확했던 주문을 먼저 확정한다. 이걸 먼저 하지 않으면 방금
   체결된 포지션이 아직 DB에 반영되지 않은 채로 다음 단계의 대조가 어긋난다.
2. **포지션 대조** — 브로커 잔고(원본, qty·평단가)와 DB `positions`
   테이블(브로커가 모르는 상태: 진입시각·이벤트ID·청산조건)을 종목별로 합친다.
   양쪽에 다 있는 종목만 안전하게 재구성할 수 있다. 한쪽에만 있으면 사람이
   확인해야 한다 — 특히 브로커에만 있는 종목은 청산 조건을 모르므로 스톱을
   걸 수 없다(01문서 §6.5, 02문서 §6 "정합성 테스트: 잔고 불일치 주입 →
   매매 중단 확인").

## 왜 매매 중단을 별도 플래그로 저장하지 않는가

`ReconcileReport.halt`는 매 사이클 확인하는 지속 상태가 아니라 **기동 시
1회 판단**이다. 미스매치가 있으면 그 결과를 반환할 뿐이고, 그걸 보고
루프에 진입할지 말지 정하는 건 호출자(미착수 `apps/live.py`)의 몫이다 —
이 모듈은 DB에 새 상태를 만들지 않는다(02문서 §7 YAGNI).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.engine import Engine

from sontrader.adapters.broker import OrderResult
from sontrader.adapters.broker_kis import KisBroker
from sontrader.core.types import Position
from sontrader.data import positions as positions_repo

MismatchReason = Literal["broker_only", "db_only"]


@dataclass(frozen=True)
class PositionMismatch:
    """한쪽에만 있는 종목 — 안전하게 재구성할 수 없어 사람이 봐야 한다."""

    symbol: str
    reason: MismatchReason
    broker_qty: int | None
    db_qty: int | None


@dataclass(frozen=True)
class ReconcileReport:
    positions: tuple[Position, ...]
    mismatches: tuple[PositionMismatch, ...]
    resolved_orders: tuple[OrderResult, ...]

    @property
    def halt(self) -> bool:
        """True면 매매를 시작하면 안 된다 — 사람이 먼저 불일치를 확인해야 한다."""
        return len(self.mismatches) > 0


def reconcile(engine: Engine, broker: KisBroker) -> ReconcileReport:
    resolved = broker.resolve_unknown()

    broker_positions = {p.symbol: p for p in broker.positions()}
    db_positions = {p.symbol: p for p in positions_repo.load_all(engine)}

    positions: list[Position] = []
    mismatches: list[PositionMismatch] = []

    for symbol, db_pos in db_positions.items():
        broker_pos = broker_positions.pop(symbol, None)
        if broker_pos is None:
            mismatches.append(PositionMismatch(symbol, "db_only", None, db_pos.qty))
            continue
        positions.append(
            Position(
                symbol=symbol,
                qty=broker_pos.qty,
                avg_price=broker_pos.avg_price,
                entered_at=db_pos.entered_at,
                exit_rule=db_pos.exit_rule,
                event_id=db_pos.event_id,
            )
        )

    # positions_repo에서 pop되지 않고 남은 항목 = DB에 기록이 없는 브로커 보유분.
    for symbol, broker_pos in broker_positions.items():
        mismatches.append(PositionMismatch(symbol, "broker_only", broker_pos.qty, None))

    return ReconcileReport(tuple(positions), tuple(mismatches), tuple(resolved))

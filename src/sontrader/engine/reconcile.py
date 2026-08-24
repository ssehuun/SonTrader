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

## DB에 쓰는 것: 해소된 체결만

기동 시 1회 판단이라는 성격상 이 모듈은 상태를 만들지 않는 게 원칙이었지만,
1번 단계의 "체결 반영"만은 예외다. 해소된 체결을 `positions`에 남기지 않으면
바로 다음 단계의 대조가 스스로 어긋나기 때문이다 — 판정 함수가 자기 입력을
오염시키는 셈이 된다.

## 왜 매매 중단을 별도 플래그로 저장하지 않는가

`ReconcileReport.halt`는 지속 상태가 아니라 **그 시점의 판단**이다. 미스매치가
있으면 그 결과를 반환할 뿐이고, 그걸 보고 루프에 진입할지 말지 정하는 건
호출자(`apps/live.py`)의 몫이다 — 이 모듈은 중단 여부를 DB에 남기지 않는다.
`apps/live.py`는 기동 시 1회가 아니라 **매 사이클** 호출한다: 장중에 계좌 밖
수동 거래가 생기는 등, 부팅 이후에도 같은 위험이 계속 있기 때문이다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.engine import Engine

from sontrader.adapters.broker import OrderResult
from sontrader.adapters.broker_kis import KisBroker
from sontrader.core.types import Position
from sontrader.data import positions as positions_repo
from sontrader.engine import fills
from sontrader.logging_setup import traced

log = logging.getLogger(__name__)

MismatchReason = Literal["broker_only", "db_only"]


@dataclass(frozen=True)
class PositionMismatch:
    """한쪽에만 있는 종목 — 안전하게 재구성할 수 없어 사람이 봐야 한다."""

    symbol: str
    reason: MismatchReason
    broker_qty: int | None
    db_qty: int | None


def _record_resolved_positions(engine: Engine, resolved: list[OrderResult]) -> None:
    """방금 확정된 체결을 `positions`에 반영한다.

    이 모듈 상단이 말하는 처리 순서 1번의 실제 알맹이다 — 이걸 하지 않으면
    "방금 체결된 포지션이 아직 DB에 반영되지 않은 채로 대조가 어긋난다"는
    바로 그 상황이 매번 벌어진다. 브로커 잔고에는 새 포지션이 있는데 DB에는
    없으니 `broker_only` 불일치가 뜨고, **체결 한 번에 매매가 영구 중단된다.**
    2026-08-20 첫 실전 운영이 이 상태였다.

    무엇을 바꿀지는 `engine/fills.py`가 판정한다. 백테스트가 같은 판정을
    메모리에 반영하므로, 규칙을 여기 복제하면 두 경로가 갈라진다.
    """
    if not resolved:
        return
    held = frozenset(p.symbol for p in positions_repo.load_all(engine))
    for change in fills.position_changes(resolved, held=held):
        if isinstance(change, fills.Opened):
            log.info(
                "포지션 신규 %s %d주 @%s (진입 %s)",
                change.symbol,
                change.qty,
                f"{change.entry_price:,}",
                change.entered_at,
            )
            positions_repo.upsert(
                engine,
                symbol=change.symbol,
                qty=change.qty,
                avg_price=float(change.entry_price),
                entered_at=change.entered_at,
                exit_rule=change.exit_rule,
                event_id=change.event_id,
            )
        else:
            log.info("포지션 청산 %s %d주 @%s", change.symbol, change.qty, f"{change.exit_price:,}")
            positions_repo.delete(engine, change.symbol)


@dataclass(frozen=True)
class ReconcileReport:
    positions: tuple[Position, ...]
    mismatches: tuple[PositionMismatch, ...]
    resolved_orders: tuple[OrderResult, ...]

    @property
    def halt(self) -> bool:
        """True면 매매를 시작하면 안 된다 — 사람이 먼저 불일치를 확인해야 한다."""
        return len(self.mismatches) > 0


@traced
def reconcile(engine: Engine, broker: KisBroker) -> ReconcileReport:
    resolved = broker.resolve_unknown()
    if resolved:
        log.info("접수 불명/미체결 주문 %d건 확정", len(resolved))
    _record_resolved_positions(engine, resolved)

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

    # 불일치는 종목별 사유까지 남긴다. 호출자(`apps/live.py`)는 "중단한다"는
    # 결정만 ERROR로 남기므로, 무엇이 왜 어긋났는지는 여기서만 알 수 있다.
    for mismatch in mismatches:
        if mismatch.reason == "broker_only":
            log.warning(
                "계좌에만 있는 종목 %s (%d주) — 청산조건을 몰라 스톱을 걸 수 없다",
                mismatch.symbol,
                mismatch.broker_qty,
            )
        else:
            log.warning(
                "DB에만 있는 종목 %s (%d주) — 계좌에 없다", mismatch.symbol, mismatch.db_qty
            )
    # 매 사이클 호출되므로 정상 경로는 DEBUG다. 살아있음은 하트비트가 답한다.
    log.debug("대조 완료: 포지션 %d건, 불일치 %d건", len(positions), len(mismatches))

    return ReconcileReport(tuple(positions), tuple(mismatches), tuple(resolved))

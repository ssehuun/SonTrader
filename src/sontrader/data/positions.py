"""포지션 영속화 — 브로커가 모르는 전략 상태만 저장한다 (구현 계획 5단계).

01문서 §6.5의 재구성 원칙: 보유 수량·평단가의 원본은 **KIS 계좌**이지 이
테이블이 아니다. 여기 저장된 `qty`/`avg_price`는 진입 시점의 스냅샷일 뿐이고,
운영 중 실제 포지션 상태는 항상 브로커 잔고조회로 복원한다. 이 테이블이 갖는
진짜 가치는 브로커가 알지 못하는 값들 — 진입 시각, 진입 시점에 확정한 청산
조건(`exit_rule_json`), 어느 이벤트로 들어왔는지(`event_id`) — 이다.

브로커 값과 합쳐 `core.types.Position`을 재구성하는 일은 `engine/reconcile.py`의
몫이다. 이 모듈은 읽기만 한다 — 진입 체결 시 이 테이블에 쓰는 코드는 아직
없다(그 책임은 미착수 상태인 `apps/live.py`의 몫이며, 지금 만들지 않는다.
YAGNI, 02문서 §7).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.core.types import ExitRule
from sontrader.data import db


@dataclass(frozen=True)
class PositionRecord:
    symbol: str
    qty: int
    avg_price: float
    entered_at: datetime
    exit_rule: ExitRule
    event_id: str | None


def load_all(engine: Engine) -> list[PositionRecord]:
    with engine.connect() as conn:
        rows = conn.execute(sa.select(db.positions)).all()
    return [_to_record(row) for row in rows]


def _to_record(row: sa.Row) -> PositionRecord:
    return PositionRecord(
        symbol=row.symbol,
        qty=row.qty,
        avg_price=float(row.avg_price),
        entered_at=row.entered_at,
        exit_rule=ExitRule.from_dict(row.exit_rule_json),
        event_id=row.event_id,
    )

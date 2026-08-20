"""포지션 영속화 — 브로커가 모르는 전략 상태만 저장한다 (구현 계획 5단계).

01문서 §6.5의 재구성 원칙: 보유 수량·평단가의 원본은 **KIS 계좌**이지 이
테이블이 아니다. 여기 저장된 `qty`/`avg_price`는 진입 시점의 스냅샷일 뿐이고,
운영 중 실제 포지션 상태는 항상 브로커 잔고조회로 복원한다. 이 테이블이 갖는
진짜 가치는 브로커가 알지 못하는 값들 — 진입 시각, 진입 시점에 확정한 청산
조건(`exit_rule_json`), 어느 이벤트로 들어왔는지(`event_id`) — 이다.

브로커 값과 합쳐 `core.types.Position`을 재구성하는 일은 `engine/reconcile.py`의
몫이다.

## 이 테이블이 비어 있으면 매매가 멈춘다

`reconcile`은 DB에 기록이 없는 브로커 보유분을 `broker_only` 불일치로 보고
매매를 중단시킨다. 그러므로 **진입 체결 시 반드시 여기 행이 생겨야 한다** —
안 그러면 봇이 한 번 사고 나서 영구 정지한다. 2026-08-20 첫 실전 운영에서
실제로 이 상태였다(쓰는 코드가 아예 없었다).

무엇을 쓸지는 `engine/fills.py`가 판정하고, 이 모듈은 저장만 한다. 백테스트가
같은 판정을 메모리에 반영하므로 규칙을 여기 두면 두 경로가 갈라진다.
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


def upsert(
    engine: Engine,
    *,
    symbol: str,
    qty: int,
    avg_price: float,
    entered_at: datetime,
    exit_rule: ExitRule,
    event_id: str | None = None,
) -> None:
    """진입 기록을 남긴다. 같은 종목이면 덮어쓴다.

    덮어쓰기가 안전한 이유: 호출자(`engine/fills.py`)가 **이미 보유 중인
    종목의 추가 체결은 변경 대상에서 제외**하므로, 여기 도달하는 것은 항상
    새 진입이다("진입 시 확정, 보유 중 불변" — 설계 3.1절). 그래도 upsert로
    두는 것은 재시작 직후 같은 체결을 두 번 반영해도 결과가 같게 하기 위해서다.
    """
    with engine.begin() as conn:
        db.upsert_rows(
            conn,
            db.positions,
            [
                {
                    "symbol": symbol,
                    "qty": qty,
                    "avg_price": avg_price,
                    "entered_at": entered_at,
                    "exit_rule_json": exit_rule.to_dict(),
                    "event_id": event_id,
                }
            ],
            key_cols=("symbol",),
        )


def delete(engine: Engine, symbol: str) -> None:
    """청산 기록. 없는 종목을 지워도 조용히 넘어간다 — 재시작 후 같은 청산을
    다시 반영하는 경우가 정상 경로에 있다."""
    with engine.begin() as conn:
        conn.execute(sa.delete(db.positions).where(db.positions.c.symbol == symbol))


def _to_record(row: sa.Row) -> PositionRecord:
    return PositionRecord(
        symbol=row.symbol,
        qty=row.qty,
        avg_price=float(row.avg_price),
        entered_at=row.entered_at,
        exit_rule=ExitRule.from_dict(row.exit_rule_json),
        event_id=row.event_id,
    )

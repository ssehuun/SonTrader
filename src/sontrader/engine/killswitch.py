"""킬 스위치 (구현 계획 6단계). 01문서 §2.5 "킬 스위치 | 텔레그램 명령".

`core/gate.py`가 청산을 절대 막지 않는 것과 같은 이유로, 킬 스위치도
**신규 진입만** 막는다 — 청산까지 멈추면 리스크 관리가 아니라 리스크
방치가 된다(설계 1.3절 집행 비대칭). 그래서 이 상태를 core에 넘기지
않는다: core는 이 플래그의 존재조차 몰라도 되고, 게이트를 통과한 목표에서
신규 진입을 걸러내는 일은 `engine/loop.py`가 한다
(`core/gate.py`의 "킬 스위치는 core에 둘 수 없다" 주석 참고).

승인 큐가 삭제되면서(01문서 §1.3) 이것이 **사람이 매매에 개입할 수 있는
유일한 지점**이 됐다. 종목을 고르는 수단이 아니라 시스템 전체를 세우는
수단이라는 점이 중요하다 — 건별 판단이 끼어들면 실전이 백테스트와
달라진다.

계좌가 하나뿐이라 전역 온/오프 하나로 충분하고, 재시작 후에도 유지돼야
하므로 단일 행(`id="singleton"`)으로 DB에 저장한다.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.data import db

_ROW_ID = "singleton"


def is_engaged(engine: Engine) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(db.kill_switch.c.engaged).where(db.kill_switch.c.id == _ROW_ID)
        ).first()
    return bool(row.engaged) if row is not None else False


def engage(engine: Engine, *, now: datetime) -> None:
    _set(engine, True, now)


def disengage(engine: Engine, *, now: datetime) -> None:
    _set(engine, False, now)


def _set(engine: Engine, engaged: bool, now: datetime) -> None:
    with engine.begin() as conn:
        existing = conn.execute(
            sa.select(db.kill_switch.c.id).where(db.kill_switch.c.id == _ROW_ID)
        ).first()
        if existing is None:
            conn.execute(
                db.kill_switch.insert().values(id=_ROW_ID, engaged=engaged, updated_at=now)
            )
        else:
            conn.execute(
                sa.update(db.kill_switch)
                .where(db.kill_switch.c.id == _ROW_ID)
                .values(engaged=engaged, updated_at=now)
            )

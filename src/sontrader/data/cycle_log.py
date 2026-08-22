"""사이클 감사 기록 (`cycle_log`).

## 왜 텍스트 로그가 아니라 DB인가

"2026-03-15에 왜 안 샀나"는 텍스트 로그를 grep해서 답할 수 있는 질문이 아니다.
슬롯이 찼는지, 쿨다운이었는지, 킬 스위치가 걸려 있었는지, 아니면 봇이 그냥
죽어 있었는지를 구분하려면 **구조화된 행**이 필요하다.

`run_cycle()`이 `CycleResult.rejections`로 그 답을 이미 만들어 놓는데,
`apps/live.py`가 반환값을 받지도 않고 버리고 있었다 — 백테스트는 이걸 모아
분석하면서 실전에는 흔적이 없는 상태였다.

## 무엇을 답할 수 있게 되는가

- **봇이 살아 있었나** — 행이 있으면 살아 있었다. 장 운영시간(08:30~15:30)에
  60초 주기이므로 하루 약 420행이 정상이고, **그 시간대의 구멍이 곧
  다운타임**이다. 장외에는 사이클 자체를 건너뛰므로(`apps/live.py`의 장
  운영시간 게이트) 밤사이 행이 없는 것은 정상이다.
- **왜 안 샀나** — `rejections`의 사유
  (SLOT_FULL / COOLDOWN / DUPLICATE_EVENT / KILL_SWITCH)
- **실전 자산 곡선** — `equity`. 백테스트의 equity_curve와 같은 형태로 비교된다.
- **언제부터 멈췄나** — `halted` 전이 시점

## 크기

장중 하루 약 420행, 연 10만 행. 무시할 수준이라 **장중에는 사이클마다 무조건**
남긴다 — "변화가 있을 때만"으로 조건을 걸면 정작 필요한 "아무 일도 없었다"는
사실이 사라진다.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.engine import Engine

from sontrader.data import db

if TYPE_CHECKING:  # 순환 import 방지 — engine이 data를 쓰고 그 반대는 아니다
    from sontrader.core.gate import Rejection


def record(
    engine: Engine,
    *,
    ts: datetime,
    watchlist_n: int,
    positions_n: int,
    cash: int,
    equity: int,
    killswitch_engaged: bool = False,
    orders_n: int = 0,
    rejections: Sequence[Rejection] = (),
    halted: bool = False,
) -> None:
    """사이클 한 건을 기록한다. 같은 시각이면 덮어쓴다 (재실행 안전)."""
    row = {
        "ts": ts,
        "watchlist_n": watchlist_n,
        "positions_n": positions_n,
        "cash": cash,
        "equity": equity,
        "killswitch_engaged": killswitch_engaged,
        "orders_n": orders_n,
        "rejections": [
            {"symbol": r.symbol, "reason": r.reason.value, "event_id": r.event_id}
            for r in rejections
        ],
        "halted": halted,
    }
    with engine.begin() as conn:
        db.upsert_rows(conn, db.cycle_log, [row], key_cols=("ts",))

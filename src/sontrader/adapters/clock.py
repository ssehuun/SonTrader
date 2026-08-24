"""Clock 어댑터 (구현 계획 5단계).

core는 시각에 접근하지 않는다(구조 원칙 1) — "지금이 언제인가"를 결정하는
책임은 여기 있다. 실전은 벽시계, 백테스트는 미리 정해둔 시각열을 순서대로
재생한다. 같은 `Clock` 프로토콜 뒤에 있으므로 `apps/live.py`와
`apps/backtest.py`가 같은 드라이버 구조를 쓸 수 있다(02문서 §2 — "engine/loop.py가
하나뿐").

미래를 미리 알 수 없다는 제약은 여기서 다루지 않는다. `Clock`은 "지금이 몇
시인가"만 답하고, 그 시각 이후의 데이터를 못 보게 막는 것은
`engine/context.py`의 `BarView` 구현이 담당한다 — 책임이 겹치지 않는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sontrader.timeutil import now_kst


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class RealClock:
    """벽시계 KST, naive datetime — DB 저장 규약과 동일(`timeutil.now_kst`)."""

    def now(self) -> datetime:
        return now_kst()


class ReplayClock:
    """사전에 정해진 시각열을 순서대로 재생한다 (백테스트).

    생성 시점에 전체 시각열을 받는다 — 다음 시각을 스스로 계산하지 않는다.
    거래일 캘린더나 봉 주기를 여기서 알아야 한다면 그 지식이 새는 것이고,
    그건 호출자(`apps/backtest.py`)가 DB에서 읽은 실제 봉 시각을 그대로
    넘기면 되는 문제다.
    """

    def __init__(self, timestamps: Sequence[datetime]) -> None:
        if not timestamps:
            raise ValueError("ReplayClock requires at least one timestamp")
        self._timestamps = list(timestamps)
        self._index = 0

    def now(self) -> datetime:
        return self._timestamps[self._index]

    def advance(self) -> bool:
        """다음 시각으로 이동한다. 더 없으면 멈추고 False를 반환한다."""
        if self._index + 1 >= len(self._timestamps):
            return False
        self._index += 1
        return True

    def __len__(self) -> int:
        return len(self._timestamps)

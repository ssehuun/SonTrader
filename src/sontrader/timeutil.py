"""시각 규약 — naive KST 벽시계 한 곳.

이 시스템의 모든 시각은 **naive KST 벽시계**다. KIS API가 그렇게 주고
(`"2026-07-31 09:00:00"`), DB도 그렇게 저장하며(`data/db.py`), 레거시
kis_trading 테이블도 같다.

**절대 맨 `datetime.now()`를 쓰지 않는다.** 그건 머신 타임존을 따르므로,
서버가 UTC면 KST 규약과 9시간 어긋난 값이 나온다. 실제로 그렇게 깨졌다:
`auth.py`가 맨 `datetime.now()`(UTC)를 KIS가 준 KST 만료 시각과 비교해서,
**토큰이 만료된 뒤에도 약 9시간 동안 유효하다고 판단**했다. KIS는 그걸
`EGW00123`("기간이 만료된 token")으로 답하는데, 원인이 시각 비교라는 단서가
어디에도 없다. 상시 가동 데몬은 24시간을 넘겨 돌므로 매일 이 구간을 지난다.

정의를 여기 하나만 둔다. 원래 `adapters/clock.py`와 `cli.py`가 각자 KST를
정의했고, 정의가 흩어져 있던 것이 `auth.py`가 규약을 우회한 채로 남아 있던
이유다. `tests/test_timeutil.py`가 맨 `datetime.now()` 사용을 막는다.

시각을 **주입받는** 계층(core, engine)은 이 모듈도 쓰지 않는다 — "지금이
언제인가"를 결정하는 책임은 `adapters/clock.py`의 `Clock`에 있고, 그래야
백테스트가 같은 코드를 재생할 수 있다(구조 원칙 1).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    """지금의 KST 벽시계, tzinfo 없는 naive datetime (DB 저장 규약과 동일)."""
    return datetime.now(KST).replace(tzinfo=None)

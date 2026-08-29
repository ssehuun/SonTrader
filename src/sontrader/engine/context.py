"""BarView 구현 — look-ahead 차단 (구현 계획 5단계).

02문서 §3.2가 요구하는 성질을 여기서 구현한다: "`BarView`가 현재 시각 이후
데이터를 **구조적으로 차단**하는 것이 중요하다. 규율에 의존하면 반드시
실수한다. 접근 자체를 불가능하게 만든다." `core/types.py`의 `BarView`
프로토콜은 타입만 정의하고, 구현은 여기 둔다 — core는 DB도 시각도 모르기
때문이다(구조 원칙 1).

## 명시적으로 확정한 것

**차단은 예외가 아니라 구조적 절단이다.** `now`를 넘는 봉은 "요청하면 막힌다"가
아니라 애초에 이 뷰의 시야에 존재하지 않는다 — `history(symbol, count)`가
count보다 적게 반환하는 것과 완전히 같은 경로다(`BarView` 프로토콜 문서:
"부족하면 있는 만큼"). 미래 데이터 부족과 과거 데이터 부족을 같은 규칙으로
다루므로 별도 예외 처리 경로가 없고, 그래서 새는 구멍도 없다. `now` 시각의
봉 자체는 포함한다(그 시각까지는 "인지된" 데이터라는 뜻 — `Context.new_events`의
`ingested_at <= now` 규약과 동일한 경계).
"""

from __future__ import annotations

import bisect
from collections.abc import Mapping, Sequence
from datetime import datetime

from sontrader.core.types import Bar


class InMemoryBarView:
    """전체 시계열을 한 번 들고 있다가, `now` 시점의 뷰를 값으로 잘라 돌려준다.

    백테스트 드라이버가 사이클마다 원본 저장소는 그대로 두고 `at(now)`으로
    새 시각의 뷰만 얻는다 — 원본 정렬 결과(`_bars`/`_timestamps`)는 시계열
    전체에 걸쳐 한 번만 계산되고 사이클마다 재사용된다.
    """

    def __init__(self, bars: Mapping[str, Sequence[Bar]], *, now: datetime | None = None) -> None:
        for symbol, rows in bars.items():
            for bar in rows:
                if bar.symbol != symbol:
                    raise ValueError(f"bar for {symbol!r} has symbol {bar.symbol!r}")
        self._bars: dict[str, list[Bar]] = {
            symbol: sorted(rows, key=lambda b: b.ts) for symbol, rows in bars.items()
        }
        self._timestamps: dict[str, list[datetime]] = {
            symbol: [bar.ts for bar in rows] for symbol, rows in self._bars.items()
        }
        # 기본값은 datetime.min(= 아무것도 안 보임)이다. look-ahead를 막는 게 이
        # 파일의 존재 이유이므로 "명시하지 않으면 전부 보인다"는 기본값은 위험하다
        # — fail-closed. 실제 사용은 항상 at(now)로 시각을 명시한 뒤에 일어난다.
        self.now = now if now is not None else datetime.min

    def at(self, now: datetime) -> InMemoryBarView:
        """새 시각으로 이동한 뷰. 원본 시계열(정렬 결과)은 공유하고 now만 바꾼다."""
        view = InMemoryBarView.__new__(InMemoryBarView)
        view._bars = self._bars
        view._timestamps = self._timestamps
        view.now = now
        return view

    def history(self, symbol: str, count: int) -> list[Bar]:
        if count <= 0:
            return []
        end = self._visible_end(symbol)
        return self._bars[symbol][max(0, end - count) : end] if end else []

    def latest(self, symbol: str) -> Bar | None:
        end = self._visible_end(symbol)
        return self._bars[symbol][end - 1] if end else None

    def _visible_end(self, symbol: str) -> int:
        """`now`까지 보이는 봉의 개수 (= 슬라이스 끝 인덱스).

        **인덱스만 돌려주고 리스트를 만들지 않는다.** 예전에는 `_visible()`이
        `bars[:idx]`로 보이는 구간 전체를 복사해 돌려줬고, `history()`가 거기서
        다시 뒤 `count`개를 잘랐다 — 일봉(종목당 약 2천 봉)에서는 눈에 띄지
        않았지만 분봉에서는 종목당 약 95,000봉이라 사이클마다 그 복사가 돈다.
        1년 분봉 재생은 약 95,000사이클 × 보유종목이라, 그대로 두면 재생 시간이
        분 단위가 아니라 시간 단위가 된다(실측: 이 변경으로 3종목 1년 재생이
        약 30배 빨라졌다).

        결과는 이전과 완전히 같다 — `now` 시각의 봉은 포함하고(bisect_right),
        그 이후는 애초에 시야에 없다.
        """
        timestamps = self._timestamps.get(symbol)
        if not timestamps:
            return 0
        return bisect.bisect_right(timestamps, self.now)

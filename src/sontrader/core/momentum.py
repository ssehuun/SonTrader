"""모멘텀 점수 (구현 계획 3단계). 순수 함수 — 수정주가 종가 시계열만 받는다.

기본값은 관례적 12-1 모멘텀: 최근 1개월(단기 반전 구간)을 건너뛰고 지난
12개월 수익률을 본다. lookback/skip은 거래일 단위이며 백테스트로 확정할
파라미터다 (설계 8절).
"""

from __future__ import annotations

from collections.abc import Sequence

LOOKBACK_BARS = 252  # 약 12개월 (거래일)
SKIP_BARS = 21  # 약 1개월 (거래일) — 단기 반전 회피


def momentum_score(
    closes: Sequence[float],
    *,
    lookback: int = LOOKBACK_BARS,
    skip: int = SKIP_BARS,
) -> float | None:
    """``closes``(날짜 오름차순)의 skip 이전 시점 대비 lookback 이전 시점 수익률.

    이력이 lookback+1개 미만이면 None — 신규 상장 종목은 점수 없이 제외된다.
    """
    if skip >= lookback:
        raise ValueError(f"skip({skip}) must be < lookback({lookback})")
    if len(closes) < lookback + 1:
        return None
    past = closes[-(lookback + 1)]
    recent = closes[-(skip + 1)]
    if past is None or recent is None or past <= 0:
        return None
    return recent / past - 1.0

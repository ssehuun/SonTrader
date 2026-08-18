"""순위 → 워치리스트, 히스테리시스 (구현 계획 3단계). 순수 함수.

편입은 상위 ``enter_rank`` 이내, 이탈은 ``exit_rank`` 밖 (설계 2.3절
원안은 50/70이었으나, 워치리스트 규모를 줄이기로 하면서 같은 비율
(5:7)로 30/42로 조정했다 — 01문서 §8 "백테스트로 결정" 대상 파라미터).
경계 종목이 매일 편입·이탈을 반복하는 깜빡임을 막는다. 동점은
종목코드 오름차순으로 깨서 같은 입력이면 항상 같은 결과가 나온다
(같은 날 재실행 시 동일 결과 요건).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass

ENTER_RANK = 30
EXIT_RANK = 42


@dataclass(frozen=True)
class WatchlistEntry:
    symbol: str
    score: float
    rank: int


def build_watchlist(
    scores: Mapping[str, float],
    previous: Collection[str],
    *,
    enter_rank: int = ENTER_RANK,
    exit_rank: int = EXIT_RANK,
) -> list[WatchlistEntry]:
    """점수 맵과 직전 워치리스트로 오늘의 워치리스트를 만든다.

    신규 편입: 순위 ≤ enter_rank. 기존 유지: 순위 ≤ exit_rank.
    반환은 순위 오름차순이며 길이는 exit_rank를 넘지 않는다.
    """
    if enter_rank > exit_rank:
        raise ValueError(f"enter_rank({enter_rank}) must be <= exit_rank({exit_rank})")
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    previous_set = set(previous)
    watchlist = []
    for rank, (symbol, score) in enumerate(ranked, start=1):
        if rank > exit_rank:
            break
        if rank <= enter_rank or symbol in previous_set:
            watchlist.append(WatchlistEntry(symbol=symbol, score=score, rank=rank))
    return watchlist

"""업종/지수 일봉 수집 (R22) — 상대 우위 판정(G3)의 입력.

## 왜 필요한가

01문서 §5.1 규칙 3이 요구하는 "시장 대비 상대 우위"를 판정하려면 **시장 쪽
시계열**이 있어야 한다. 전략이 +10%를 냈어도 같은 기간 지수가 +15%였다면
그건 우위가 아니다. 지금까지 그 비교를 한 번도 못 했다 — 지수 데이터가
아예 없었기 때문이다.

## 🔴 소급 수집이 된다 (2026-08-27 실측)

착수 전 전제는 "소급이 안 되니 오늘부터 기록해야 한다"였는데 **틀렸다.**
2019년 1월 구간을 요청해 실제로 받았다. 분봉(1년 보관)과 다르다.

**따라서 G3는 과거 구간에도 적용할 수 있다.** 포워드 검정을 기다릴 필요가
없고, 기존 일봉 백테스트(2019~)에도 같은 기준을 소급해 댈 수 있다.

## 페이징 — 요청 구간이 아니라 **건수**가 상한이다

응답은 요청 구간과 무관하게 **최신 50건**만 온다(실측: 2019-01-01\\~03-31을
요청하면 그 안의 마지막 50거래일인 01-15\\~03-29가 온다).

그래서 `data/prices.py`의 "날짜 창을 앞으로 밀며 수집"과 방향이 반대다 —
여기서는 **`end`를 과거로 물려 가며** 받는다. 창 길이를 넉넉히 잡고 받은 것
중 가장 오래된 날짜의 하루 전을 다음 `end`로 삼는다.

## 값은 소수점을 갖는다

지수는 `6912.37`처럼 소수 2자리다. `stock_candles_1d`가 Integer 컬럼이라
거기 담을 수 없고, 담아서도 안 된다 — 그래서 `index_candles_1d`가 따로 있다
(`data/db.py` 참고).
"""

from __future__ import annotations

import logging
import time as time_module
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.data import db

log = logging.getLogger(__name__)

# 업종코드. **응답의 `hts_kor_isnm`으로 실측 확인했다 (2026-08-28)** — 처음에
# `2001`을 KOSDAQ으로 알고 수집했는데 그것은 **KOSPI200**이었다. 이름을 안
# 물어봤으면 조용히 틀린 지수로 상대 우위를 판정했을 것이다.
#
# | 코드 | `hts_kor_isnm` |
# |---|---|
# | 0001 | 종합 (KOSPI) |
# | 0002 | 대형주 |
# | 1001 | KOSDAQ |
# | 2001 | KOSPI200 |
#
# 잡아낸 단서는 스케일이었다: `2001`의 1996-07-01(코스닥 개장, 기준 1000) 값이
# 91이었고 2000년 닷컴 최고가 134였다 — 코스닥이라면 각각 1000과 2,925여야 한다.
KOSPI = "0001"
KOSDAQ = "1001"
KOSPI200 = "2001"

# 한 응답의 건수 상한 (실측 2026-08-27). 요청 구간을 아무리 넓혀도 이보다
# 많이 오지 않는다 — 구간이 아니라 건수가 상한이라는 것이 핵심이다.
PAGE_SIZE = 50

# 한 번에 요청할 날짜 창. 50거래일이 약 70달력일이라 여유를 두고 잡는다.
# 짧으면 호출이 늘고, 길면 앞쪽이 잘려 버려지는 구간이 생긴다(응답이 최신
# 50건만 오므로 창 앞부분은 어차피 안 온다).
WINDOW_DAYS = 70

# 호출 상한. 상한이 없으면 API가 예상과 다르게 굴 때(같은 페이지 반복)
# 무한히 돈다.
#
# **처음에 200으로 잡았다가 늘렸다.** 2019년까지만 생각한 값이었는데(약
# 1,900거래일 ÷ 50 ≈ 38회) 지수는 훨씬 길다 — KOSPI는 1980년대까지 있고,
# 창 하나가 70달력일 ≈ 48거래일이라 40년이면 200을 넘긴다. 실제로 넘겼다면
# 조용히 잘린 채 "수집 완료"로 보였을 것이다.
#
# 500이면 약 95년치다. 그 뒤에는 어차피 데이터가 없어 빈 응답이 먼저 끝낸다.
MAX_PAGES = 500


class IndexCandleSource(Protocol):
    """`KisClient`가 만족하는 지수 조회 (테스트에서는 스텁으로 대체)."""

    def get_index_daily_candles(
        self, code: str, start: date, end: date
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class IndexCollectResult:
    code: str
    rows: int
    pages: int
    oldest: date | None
    newest: date | None


def collect_index(
    engine: Engine,
    client: IndexCandleSource,
    code: str = KOSPI,
    *,
    end: date,
    earliest: date | None = None,
    pace_seconds: float = 0.0,
    sleep: Callable[[float], None] = time_module.sleep,
) -> IndexCollectResult:
    """`end`부터 과거로 지수 일봉을 받는다. 재실행해도 결과가 같다 (upsert).

    `earliest`를 주면 거기까지만 받는다. 주지 않으면 **API가 빈 응답을 줄
    때까지** 간다 — 보관 경계를 상수로 박지 않는 것이 `data/minutes.py`와 같은
    판단이다.

    이미 저장된 구간에 닿아도 **멈추지 않는다.** 지수는 종목과 달리 결손이
    드물지만, 멈추는 최적화를 넣으면 저장 구간 안의 구멍을 영영 못 메운다
    (분봉 수집기가 실제로 그 문제를 겪었다 — `data/minutes.py` 참고).
    """
    cursor = end
    pages = 0
    total = 0
    oldest: date | None = None
    newest: date | None = None

    while pages < MAX_PAGES:
        if pace_seconds and pages:
            sleep(pace_seconds)
        window_start = cursor - timedelta(days=WINDOW_DAYS)
        if earliest is not None and window_start < earliest:
            window_start = earliest
        if window_start > cursor:
            break

        raw = client.get_index_daily_candles(code, window_start, cursor)
        pages += 1
        rows = _parse(code, raw)
        if not rows:
            log.debug("%s %s 이전 지수 데이터 없음 — 수집 종료", code, cursor)
            break

        total += _upsert(engine, rows)
        page_oldest = rows[0]["date"]
        page_newest = rows[-1]["date"]
        oldest = page_oldest if oldest is None else min(oldest, page_oldest)
        newest = page_newest if newest is None else max(newest, page_newest)

        if earliest is not None and page_oldest <= earliest:
            break
        # 다음 창은 이번에 받은 가장 오래된 날의 **하루 전**까지다. 같은 날을
        # 다시 받으면 진행이 없어 무한 루프가 된다.
        next_cursor = page_oldest - timedelta(days=1)
        if next_cursor >= cursor:
            log.warning("%s 커서가 진행하지 않는다 (%s) — 수집 중단", code, cursor)
            break
        cursor = next_cursor
    else:
        log.warning("%s 호출 상한 %d회 도달 — 수집을 끊는다", code, MAX_PAGES)

    if total:
        log.info("%s 지수 %d건 저장 (%s ~ %s, 호출 %d회)", code, total, oldest, newest, pages)
    return IndexCollectResult(code=code, rows=total, pages=pages, oldest=oldest, newest=newest)


def load_closes(engine: Engine, code: str, start: date, end: date) -> dict[date, float]:
    """날짜 → 종가. G3 판정이 읽는 지점이다."""
    columns = db.index_candles_1d.c
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(columns.date, columns.close).where(
                columns.code == code, columns.date >= start, columns.date <= end
            )
        )
        return {row.date: row.close for row in rows}


def _parse(code: str, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """응답 → 저장 행. 시각 오름차순.

    **종가가 없는 행은 버린다.** 지수 종가는 G3 판정의 유일한 입력이라, 없는
    채로 넣으면 그 날짜만 조용히 비교에서 빠진다.
    """
    parsed: list[dict[str, Any]] = []
    for row in raw:
        try:
            day = date(
                int(row["stck_bsop_date"][:4]),
                int(row["stck_bsop_date"][4:6]),
                int(row["stck_bsop_date"][6:8]),
            )
            close = float(row["bstp_nmix_prpr"])
        except (KeyError, TypeError, ValueError):
            continue
        parsed.append(
            {
                "code": code,
                "date": day,
                "open": _opt_float(row.get("bstp_nmix_oprc")),
                "high": _opt_float(row.get("bstp_nmix_hgpr")),
                "low": _opt_float(row.get("bstp_nmix_lwpr")),
                "close": close,
                "volume": _opt_int(row.get("acml_vol")),
                "trade_value": _opt_int(row.get("acml_tr_pbmn")),
            }
        )
    parsed.sort(key=lambda r: r["date"])
    return parsed


def _opt_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _upsert(engine: Engine, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    with engine.begin() as conn:
        db.upsert_rows(conn, db.index_candles_1d, rows, key_cols=("code", "date"))
    return len(rows)

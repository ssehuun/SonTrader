"""분봉 수집기 — 거래소 공식 1분봉, 백테스트용.

일봉 수집기(`data/prices.py`)와 **별 모듈로 둔다.** 같은 "봉 수집"이지만
API·페이징·정합성 문제가 전부 다르다:

| | 일봉 | 분봉 |
|---|---|---|
| TR | FHKST03010100 (모의 지원) | FHKST03010230 (**모의 미지원**) |
| 수정주가 | `FID_ORG_ADJ_PRC="0"` | **파라미터 없음 = 원주가** |
| 페이징 | 날짜 구간 100일 창 | 기준 시각에서 과거로 120건 |
| 보관 | 수년 | **1년** |
| 자가치유 | 기업행위 감지 시 종목 전체 재수집 | 불필요 (소급 재계산이 없다) |

## 왜 자가치유가 없는가

일봉은 수정주가라서 기업행위 시점에 과거 전체가 소급 재계산된다 — 그래서
증분 수집만 하면 저장분과 신규분의 기준이 갈라진다. 분봉은 원주가라 이미
저장된 값이 나중에 바뀌지 않는다. 대신 **일봉과 분봉의 가격 기준이 어긋나는**
별개 문제가 생긴다(액면분할 전후). `todo/02-매매-정교화.md`에 기록해 두고
이번 범위에서는 다루지 않는다.

## 페이징

응답은 최신이 먼저인 내림차순이고, 요청한 기준 시각을 **포함**한다. 다음
요청의 기준을 "받은 것 중 가장 오래된 봉의 시각"으로 두면 그 봉 하나가
겹치는데, 겹침은 upsert가 흡수하고 연속성 확인에 쓸 수 있다.

**API가 날짜 경계를 스스로 넘는다** — 기준 08-24 09:10으로 요청하면 08-24
11건 + 08-21 109건으로 120건을 채워 돌려준다(실측). 그래서 레거시
kis_trading 수집기의 "120건 미만이면 그날은 끝났으니 전날 자정으로 점프"
휴리스틱은 **가져오지 않는다** — 불필요하고, 부분 페이지를 받았을 때 그날
남은 봉을 건너뛴다.

무한 루프 가드는 필요하다. 같은 페이지가 되돌아오면(가장 오래된 봉이 기준
시각보다 늦거나 같으면) 기준을 전날 자정으로 강제로 물린다.

## 거래대금

`acml_tr_pbmn`은 **그날 시작부터의 누적값**이고 날짜마다 리셋된다(실측:
08-21 15:30 7.68조 → 08-24 09:00 2,298억). 봉별 값은 같은 날 안에서 연속
차분으로 구하고, 그날 첫 봉은 누적값을 그대로 쓴다.
"""

from __future__ import annotations

import logging
import time as time_module
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol

import httpx
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.auth import is_transient
from sontrader.client import KisError
from sontrader.data import db
from sontrader.logging_setup import traced

log = logging.getLogger(__name__)

# 기본 수집 범위 = **서버가 가진 만큼 전부**. 기간을 지정하지 않는 것이 정상
# 사용법이다 — 분봉은 애초에 1년만 보관되므로 "얼마나 소급할지"는 사용자가
# 정할 게 아니라 서버가 정한다.
#
# 실측(2026-08-25, 005930): 365일 전은 데이터가 있고 380일 전부터 0건 —
# 경계는 365~379일 사이다. 롤링 경계라 상수로 못 박으면 시간이 지나며
# 어긋나므로, 넉넉히 넘겨 잡고 **실제 종료는 API의 빈 응답이 결정한다**
# (그게 이미 종료 조건이다). 365로 잘라내면 경계 근처 최대 2주를 이유 없이
# 버린다.
#
# 상한 역할도 겸한다 — 오타(`--days 36500`)로 호출 상한까지 도는 것을 막는다.
MAX_DAYS = 400

# 1회 응답 상한. 이보다 적게 오는 것은 정상이다(보관 경계, 결손 구간).
PAGE_SIZE = 120

MAX_CONSECUTIVE_FAILURES = 10

# 한 종목에 허용할 최대 호출 수. 하루 약 391분 ÷ 120건 ≈ 4호출, 1년 약 245
# 거래일이면 약 980호출이다. 상한이 없으면 API가 예상과 다르게 굴 때(같은
# 페이지 반복 등) 한 종목이 유량을 다 태운다.
MAX_PAGES_PER_SYMBOL = 1_500

_RETRIES = 3
_RETRY_BACKOFF = 1.0


class MinuteCandleSource(Protocol):
    """KisClient가 만족하는 분봉 소스 (테스트에서는 스텁으로 대체)."""

    def get_intraday_candles(self, code: str, reference: datetime) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class PageProgress:
    """API 호출 한 번의 결과. 호출자가 진행도를 보여주는 데 쓴다.

    로그가 아니라 콜백인 이유: `cli.py`는 `logging_setup.configure()`를 부르지
    않는다(그 모듈의 `print`는 로그가 아니라 명령어의 출력이다). 그래서 이
    모듈의 `log.info`는 CLI 실행에서 보이지 않는다 — 일봉 수집기의
    `on_progress`와 같은 방식으로 호출자에게 넘긴다.
    """

    symbol: str
    page: int  # 이 종목의 몇 번째 호출인가
    rows: int  # 이번 응답 건수 (120이 상한)
    reached: datetime  # 이번 페이지에서 도달한 가장 오래된 봉
    started: datetime  # 수집 시작 시각 (진행률 분모)
    floor: datetime  # 수집 하한
    stored: int  # 이 종목에서 지금까지 저장한 행 수

    @property
    def percent(self) -> float:
        """하한까지 얼마나 왔는가. 종목당 약 980호출이라 이게 없으면
        언제 끝날지 짐작할 수 없다."""
        span = (self.started - self.floor).total_seconds()
        if span <= 0:
            return 100.0
        done = (self.started - self.reached).total_seconds()
        return max(0.0, min(100.0, done / span * 100.0))


@dataclass(frozen=True)
class MinuteCollectResult:
    symbol: str
    rows: int  # upsert한 행 수
    pages: int  # API 호출 수
    oldest: datetime | None  # 저장한 가장 오래된 봉
    newest: datetime | None


class MinuteCollectionAborted(RuntimeError):
    """연속 실패로 수집을 중단했다. 중단 시점까지의 결과를 함께 싣는다."""

    def __init__(
        self,
        message: str,
        *,
        results: list[MinuteCollectResult] | None = None,
        failures: list[tuple[str, Exception]] | None = None,
    ) -> None:
        super().__init__(message)
        self.results = results or []
        self.failures = failures or []


@traced
def collect_minutes(
    engine: Engine,
    client: MinuteCandleSource,
    symbol: str,
    *,
    now: datetime,
    days: int = MAX_DAYS,
    pace_seconds: float = 0.0,
    sleep: Callable[[float], None] = time_module.sleep,
    on_page: Callable[[PageProgress], None] | None = None,
) -> MinuteCollectResult:
    """한 종목의 분봉을 과거로 수집한다. 재실행해도 결과가 같다 (upsert).

    `now`부터 과거로 `days`일까지. 이미 채운 구간(`source='rest'`)에 들어오면
    그 구간을 **건너뛰고 아래를 이어 받는다** — 거기서 멈추면 저장분보다
    오래된 쪽에 구멍이 남고 다시는 채워지지 않는다.

    장 운영시간은 알지 않는다 — `now`를 정하는 것은 호출자의 몫이다.
    """
    floor = _floor(now, days)
    stored_newest = _last_stored(engine, symbol)
    stored_oldest = _first_stored(engine, symbol)

    reference = now
    pages = 0
    total_rows = 0
    oldest_saved: datetime | None = None
    newest_saved: datetime | None = None

    while pages < MAX_PAGES_PER_SYMBOL:
        if pace_seconds:
            sleep(pace_seconds)
        raw = _call_with_retry(client, symbol, reference, sleep=sleep)
        pages += 1
        if not raw:
            # 보관 경계에 닿았거나 그 이전에 데이터가 없다.
            log.debug("%s %s 이전 데이터 없음 — 수집 종료", symbol, reference)
            break

        rows = _parse_page(symbol, raw)
        if not rows:
            log.warning("%s %s 응답 %d건이 전부 파싱 실패 — 수집 중단", symbol, reference, len(raw))
            break

        keep = [row for row in rows if row["ts"] >= floor]
        # 페이지의 **가장 오래된 봉은 저장하지 않는다.** 그 봉은 직전 봉이 이
        # 페이지에 없어 거래대금 차분이 불가능하고, `_parse_page`가 누적값을
        # 그대로 넣는다(그날 첫 봉으로 취급). 겹침 덕분에 다음 페이지에서
        # '가장 최신 봉'으로 다시 오고, 그때는 직전 봉이 함께 있어 올바른
        # 차분이 나온다.
        #
        # 연속 페이징 중에는 다음 페이지가 덮어써서 문제가 드러나지 않았고,
        # **증분 수집으로 한 페이지에서 멈출 때만** 잘못된 값이 남았다 —
        # 실제로 005930 13:21 봉이 정상값의 714배로 저장됐다.
        #
        # `keep[0]`이 페이지의 최고(最古) 봉일 때만 뺀다. 하한 필터로 앞쪽이
        # 잘렸다면 `keep[0]`에는 직전 봉이 있었으므로 차분이 이미 옳다.
        # 봉이 하나뿐이면 더 나은 선택이 없어 그대로 둔다(다음 호출이 덮는다).
        if len(keep) > 1 and keep[0]["ts"] == rows[0]["ts"]:
            keep = keep[1:]
        if keep:
            total_rows += _upsert(engine, keep)
            oldest_saved = min(keep[0]["ts"], oldest_saved or keep[0]["ts"])
            newest_saved = max(keep[-1]["ts"], newest_saved or keep[-1]["ts"])

        page_oldest = rows[0]["ts"]
        if on_page is not None:
            on_page(
                PageProgress(
                    symbol=symbol,
                    page=pages,
                    rows=len(rows),
                    reached=page_oldest,
                    started=now,
                    floor=floor,
                    stored=total_rows,
                )
            )
        if page_oldest <= floor:
            break  # 목표 구간을 다 덮었다
        if stored_newest is not None and page_oldest <= stored_newest:
            # 이미 채운 구간에 들어왔다. **멈추지 않고 그 구간 아래로 건너뛴다.**
            #
            # 수집은 과거로 가는데 판정 기준이 '가장 최신 저장 봉'이라, 여기서
            # 멈추면 저장분보다 오래된 쪽에 구멍이 남고 다시는 채워지지 않는다.
            # 실제로 그렇게 됐다 — 앞선 실행이 잘려 000660에 08-24 15:09 위쪽만
            # 있었고, 다음 실행은 1호출 만에 "이미 있다"며 끝나 그 아래를
            # 영원히 비워 뒀다.
            stored_newest = None  # 건너뛰기는 한 번만 (무한 반복 방지)
            if stored_oldest is not None and stored_oldest > floor:
                log.debug("%s 저장 구간을 건너뛰고 %s 이전을 계속 받는다", symbol, stored_oldest)
                reference = stored_oldest
                continue
            log.debug("%s 저장분이 하한까지 덮여 있다 — 증분 수집 종료", symbol)
            break

        reference = _next_reference(page_oldest, reference)
        if reference <= floor:
            # 기준이 하한을 지났다. 정상 종료 경로이기도 하고, 위 무한 루프
            # 가드가 기준을 하루씩 물릴 때의 종료 조건이기도 하다 — 가드만
            # 있으면 루프는 안 돌지만 상한(MAX_PAGES_PER_SYMBOL)까지 간다.
            break

    else:
        log.warning(
            "%s 호출 상한 %d회 도달 — 수집을 끊는다 (기준 %s)",
            symbol,
            MAX_PAGES_PER_SYMBOL,
            reference,
        )

    if total_rows:
        log.info(
            "%s 분봉 %d건 저장 (%s ~ %s, 호출 %d회)",
            symbol,
            total_rows,
            oldest_saved,
            newest_saved,
            pages,
        )
    return MinuteCollectResult(
        symbol=symbol, rows=total_rows, pages=pages, oldest=oldest_saved, newest=newest_saved
    )


def collect_minutes_all(
    engine: Engine,
    client: MinuteCandleSource,
    symbols: Sequence[str],
    *,
    now: datetime,
    days: int = MAX_DAYS,
    pace_seconds: float = 0.0,
    sleep: Callable[[float], None] = time_module.sleep,
    on_progress: Callable[[int, int], None] | None = None,
    on_page: Callable[[PageProgress], None] | None = None,
) -> tuple[list[MinuteCollectResult], list[tuple[str, Exception]]]:
    """여러 종목을 순차 수집한다. 한 종목의 실패는 다음 종목을 막지 않는다.

    다만 **연속 실패가 이어지면 중단한다** — 앱키 무효·유량 설계 오류처럼
    공통 원인으로 전부 실패하는 상황에서 남은 종목을 계속 시도하면 시간과
    API 유량만 태운다(일봉 수집기와 같은 판단).
    """
    results: list[MinuteCollectResult] = []
    failures: list[tuple[str, Exception]] = []
    streak = 0

    for index, symbol in enumerate(symbols, start=1):
        try:
            results.append(
                collect_minutes(
                    engine,
                    client,
                    symbol,
                    now=now,
                    days=days,
                    pace_seconds=pace_seconds,
                    sleep=sleep,
                    on_page=on_page,
                )
            )
            streak = 0
        except (KisError, httpx.HTTPError, sa.exc.SQLAlchemyError) as exc:
            failures.append((symbol, exc))
            streak += 1
            log.error("%s 분봉 수집 실패 (%d회 연속): %s", symbol, streak, exc)
            if streak >= MAX_CONSECUTIVE_FAILURES:
                raise MinuteCollectionAborted(
                    f"{streak}회 연속 실패 — 공통 원인으로 보고 중단합니다",
                    results=results,
                    failures=failures,
                ) from exc
        if on_progress is not None:
            on_progress(index, len(symbols))

    return results, failures


# --- 내부 -------------------------------------------------------------------


def _floor(now: datetime, days: int) -> datetime:
    """수집 하한.

    365로 자르지 않는다 — 실제 보관 경계가 365~379일 사이라 365로 자르면
    최대 2주를 이유 없이 버린다. 보관 밖은 API의 빈 응답으로 끝난다.
    """
    return now - timedelta(days=min(days, MAX_DAYS))


def _next_reference(page_oldest: datetime, current: datetime) -> datetime:
    """다음 요청 기준 시각.

    보통은 이번 페이지의 가장 오래된 봉이다 — 그 봉 하나가 겹치지만 upsert가
    흡수한다. 다만 그것이 현재 기준보다 늦거나 같으면 같은 페이지가 되돌아온
    것이라 그대로 두면 무한 루프가 된다. 그때는 전날 자정으로 강제로 물린다.
    """
    if page_oldest < current:
        return page_oldest
    return datetime.combine(current.date(), datetime.min.time()) - timedelta(minutes=1)


def _call_with_retry(
    client: MinuteCandleSource,
    symbol: str,
    reference: datetime,
    *,
    sleep: Callable[[float], None],
) -> list[dict[str, Any]]:
    """일시 오류(유량 초과 등)는 재시도한다. `client.py`의 재시도와 별개로
    여기 두는 이유는 일봉 수집기와 같다 — 종목 단위 루프에서 한 번의 유량
    초과로 그 종목을 통째로 잃지 않기 위해서다."""
    for attempt in range(1, _RETRIES + 1):
        try:
            return client.get_intraday_candles(symbol, reference)
        except KisError as exc:
            if not is_transient(exc) or attempt == _RETRIES:
                raise
            log.warning("%s 일시 오류 재시도 %d/%d: %s", symbol, attempt, _RETRIES, exc)
            sleep(_RETRY_BACKOFF * attempt)
    raise AssertionError("unreachable")


def _parse_page(symbol: str, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """응답 한 페이지 → 저장 행. 시각 **오름차순**으로 돌려준다.

    거래대금은 같은 날 안에서 누적값의 차분이다. 페이지 경계에서 전 봉이
    없을 수 있으므로, 그날의 첫 봉(또는 페이지에서 그날 가장 이른 봉)은
    누적값을 그대로 쓴다 — 09:00 봉의 누적값이 그 봉 자체의 거래대금과
    일치하는 것을 실측으로 확인했다.
    """
    parsed: list[dict[str, Any]] = []
    for row in raw:
        ts = _parse_ts(row)
        if ts is None:
            continue
        try:
            parsed.append(
                {
                    "symbol": symbol,
                    "ts": ts,
                    "open": int(row["stck_oprc"]),
                    "high": int(row["stck_hgpr"]),
                    "low": int(row["stck_lwpr"]),
                    "close": int(row["stck_prpr"]),
                    "volume": int(row["cntg_vol"]),
                    # 거래소 확정 봉이다. 웹소켓 집계분(`ws`)과 값이 갈리므로
                    # 출처를 남긴다 — 백테스트는 `rest`만 읽는다(`data/db.py`).
                    "source": "rest",
                    "_acml": int(row["acml_tr_pbmn"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue  # 결손 필드가 있는 행은 버린다 — 있는 만큼만 쓴다

    parsed.sort(key=lambda r: r["ts"])
    # 직전 봉의 누적값을 따로 들고 간다. 행에서 바로 꺼내면 앞 행의 `_acml`을
    # 이미 지운 상태라 KeyError가 난다 — 실제로 그렇게 틀렸다.
    prev_acml: int | None = None
    prev_day: date | None = None
    for row in parsed:
        acml = row.pop("_acml")
        day = row["ts"].date()
        same_day = prev_acml is not None and day == prev_day
        row["trade_value"] = acml - prev_acml if same_day else acml
        prev_acml, prev_day = acml, day
    return parsed


def _parse_ts(row: dict[str, Any]) -> datetime | None:
    day = row.get("stck_bsop_date")
    hhmmss = row.get("stck_cntg_hour")
    if not day or not hhmmss:
        return None
    try:
        return datetime.strptime(f"{day}{hhmmss}", "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _last_stored(engine: Engine, symbol: str) -> datetime | None:
    """이 수집기가 이미 채운 가장 최근 봉. **`source='rest'`만 센다.**

    `source`를 구분하지 않으면 웹소켓 집계 봉(`ws`)이 증분 종료를 앞당겨
    REST 수집을 잘라먹는다. 실제로 그렇게 잘렸다 — 000660은 ws 봉이 08-24
    15:54까지 있어서, 2호출(08-24 15:08 도달) 만에 "이미 수집했다"고 판단하고
    하한(08-22)에 한참 못 미친 30%에서 멈췄다. 데몬이 스트리밍한 종목은 전부
    이렇게 잘리는데, 그게 정확히 백테스트에 필요한 종목들이다.
    """
    columns = db.stock_candles_1m.c
    with engine.connect() as conn:
        return conn.execute(
            sa.select(sa.func.max(columns.ts)).where(
                columns.symbol == symbol, columns.source == "rest"
            )
        ).scalar_one_or_none()


def _first_stored(engine: Engine, symbol: str) -> datetime | None:
    """이 수집기가 채운 가장 오래된 봉. `_last_stored`와 같은 이유로
    `source='rest'`만 센다.

    증분 수집이 이미 채운 구간을 **건너뛸** 지점이다 — 과거로 내려가다 저장
    구간에 들어오면 여기로 점프해 그 아래를 이어 받는다.
    """
    columns = db.stock_candles_1m.c
    with engine.connect() as conn:
        return conn.execute(
            sa.select(sa.func.min(columns.ts)).where(
                columns.symbol == symbol, columns.source == "rest"
            )
        ).scalar_one_or_none()


def _upsert(engine: Engine, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    with engine.begin() as conn:
        db.upsert_rows(conn, db.stock_candles_1m, rows, key_cols=("symbol", "ts"))
    return len(rows)


def stored_days(engine: Engine, symbol: str) -> list[date]:
    """저장된 영업일 목록 — 백테스트의 사이클 날짜 후보."""
    columns = db.stock_candles_1m.c
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(sa.func.date(columns.ts)).where(columns.symbol == symbol).distinct()
        )
        return sorted(row[0] for row in rows)

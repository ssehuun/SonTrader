"""일봉 수집기 — 수정주가 기준, 기업행위 자가치유 (구현 계획 3단계).

레거시 kis_trading 수집기의 두 가지 문제(0단계 검증)를 해결한다:

1. **수정주가로 수집한다** (FID_ORG_ADJ_PRC="0"). 원주가 수집은 액면분할
   시 시계열에 보정 불가능한 단절을 남겨 모멘텀 점수를 왜곡한다.
2. **기업행위를 감지하면 해당 종목 전체를 재수집한다.** 수정주가는 기업행위
   시점에 과거 전체가 소급 재계산되므로, 증분 수집만 하면 저장분(구 기준)과
   신규분(신 기준)이 갈라진다. 증분 수집 시 최근 구간을 겹쳐 받아 저장된
   종가와 대조하고, 하나라도 다르면 그 종목을 지우고 처음부터 다시 받는다.

유량 제한: KIS는 실전 초당 20건 / 모의 초당 2건. 호출 사이 간격(pace)은
API 호출 직전마다 적용한다 — 종목 단위가 아니라 호출 단위가 맞다.
"""

from __future__ import annotations

import time as time_module
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol

import httpx
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.client import KisError
from sontrader.data import db

LOOKBACK_DAYS = 420  # 12-1 모멘텀(약 13개월) + 휴장 여유
OVERLAP_DAYS = 10  # 증분 수집 시 소급 수정 감지용 겹침 (달력일)
WINDOW_DAYS = 100  # 호출당 조회 폭 — 100행 응답 한도를 달력일로 안전하게 커버


class DailyCandleSource(Protocol):
    """KisClient가 만족하는 시세 소스 (테스트에서는 스텁으로 대체)."""

    def get_daily_candles(
        self, code: str, start: date, end: date, adjusted: bool = True
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class CollectResult:
    symbol: str
    rows: int  # upsert한 행 수
    full: bool  # 전체 수집이었는지 (기업행위 재수집 포함)


def collect_daily(
    engine: Engine,
    client: DailyCandleSource,
    symbol: str,
    *,
    today: date,
    lookback_days: int = LOOKBACK_DAYS,
    pace_seconds: float = 0.0,
    sleep: Callable[[float], None] = time_module.sleep,
) -> CollectResult:
    """한 종목의 일봉을 증분 수집한다. 재실행해도 결과가 같다 (upsert)."""
    last_date = _last_stored_date(engine, symbol)
    full = last_date is None
    if full:
        start = today - timedelta(days=lookback_days)
    else:
        start = last_date - timedelta(days=OVERLAP_DAYS)

    fetched = _fetch_range(client, symbol, start, today, pace_seconds, sleep)

    if not full and _was_adjusted(engine, symbol, fetched, today=today):
        # 겹침 구간이 저장분과 다르다 = 기업행위로 과거가 소급 수정됨.
        # 전체 이력을 **먼저 받고, 성공한 뒤에야** 한 트랜잭션으로 교체한다 —
        # 지우고 나서 받다가 실패하면 종목 이력이 통째로 사라지기 때문.
        full_rows = _fetch_range(
            client, symbol, today - timedelta(days=lookback_days), today, pace_seconds, sleep
        )
        _replace_symbol(engine, symbol, full_rows)
        return CollectResult(symbol=symbol, rows=len(full_rows), full=True)

    _upsert(engine, fetched)
    return CollectResult(symbol=symbol, rows=len(fetched), full=full)


def collect_daily_all(
    engine: Engine,
    client: DailyCandleSource,
    symbols: list[str],
    *,
    today: date,
    lookback_days: int = LOOKBACK_DAYS,
    pace_seconds: float = 0.0,
    sleep: Callable[[float], None] = time_module.sleep,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[CollectResult], list[tuple[str, Exception]]]:
    """여러 종목 수집. 한 종목의 실패가 전체를 중단시키지 않는다."""
    results: list[CollectResult] = []
    failures: list[tuple[str, Exception]] = []
    for index, symbol in enumerate(symbols, start=1):
        try:
            results.append(
                collect_daily(
                    engine,
                    client,
                    symbol,
                    today=today,
                    lookback_days=lookback_days,
                    pace_seconds=pace_seconds,
                    sleep=sleep,
                )
            )
        except Exception as exc:  # noqa: BLE001 — 종목 단위 격리가 목적
            failures.append((symbol, exc))
        if on_progress is not None:
            on_progress(index, len(symbols))
    return results, failures


def _fetch_range(
    client: DailyCandleSource,
    symbol: str,
    start: date,
    end: date,
    pace_seconds: float,
    sleep: Callable[[float], None],
) -> list[dict[str, Any]]:
    """[start, end]를 100일 창으로 나눠 전부 받아 표준 행으로 변환한다."""
    rows: list[dict[str, Any]] = []
    window_start = start
    while window_start <= end:
        window_end = min(window_start + timedelta(days=WINDOW_DAYS - 1), end)
        if pace_seconds > 0:
            sleep(pace_seconds)
        for raw in _call_with_retry(client, symbol, window_start, window_end, sleep):
            parsed = _parse_row(symbol, raw)
            if parsed is not None:
                rows.append(parsed)
        window_start = window_end + timedelta(days=1)
    # 창 경계가 겹칠 일은 없지만, 같은 날짜가 두 번 오면 마지막 것이 이긴다.
    unique = {(r["symbol"], r["date"]): r for r in rows}
    return sorted(unique.values(), key=lambda r: r["date"])


_RETRIES = 3


def _call_with_retry(
    client: DailyCandleSource,
    symbol: str,
    start: date,
    end: date,
    sleep: Callable[[float], None],
) -> list[dict[str, Any]]:
    """일시 오류(KIS 5xx, 유량 초과 EGW00201)만 재시도한다."""
    for attempt in range(1, _RETRIES + 1):
        try:
            return client.get_daily_candles(symbol, start, end)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500 or attempt == _RETRIES:
                raise
        except httpx.TransportError:
            if attempt == _RETRIES:
                raise
        except KisError as exc:
            if "EGW00201" not in str(exc) or attempt == _RETRIES:
                raise
        sleep(float(attempt))  # 선형 백오프
    raise AssertionError("unreachable")


def _parse_row(symbol: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    try:
        day = datetime.strptime(raw["stck_bsop_date"], "%Y%m%d").date()
    except (KeyError, ValueError, TypeError):
        return None
    return {
        "symbol": symbol,
        "date": day,
        "open": _to_int(raw.get("stck_oprc")),
        "high": _to_int(raw.get("stck_hgpr")),
        "low": _to_int(raw.get("stck_lwpr")),
        "close": _to_int(raw.get("stck_clpr")),
        "volume": _to_int(raw.get("acml_vol")),
        "trade_value": _to_int(raw.get("acml_tr_pbmn")),
        "flng_cls_code": (raw.get("flng_cls_code") or "").strip() or None,
        "prtt_rate": _to_float(raw.get("prtt_rate")),
        "mod_yn": (raw.get("mod_yn") or "").strip() or None,
    }


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


def _last_stored_date(engine: Engine, symbol: str) -> date | None:
    with engine.connect() as conn:
        return conn.execute(
            sa.select(sa.func.max(db.stock_candles_1d.c.date)).where(
                db.stock_candles_1d.c.symbol == symbol
            )
        ).scalar_one()


def _was_adjusted(
    engine: Engine, symbol: str, fetched: list[dict[str, Any]], *, today: date
) -> bool:
    """겹침 구간에서 저장분과 (종가, 거래량)이 하나라도 다르면 소급 수정으로 판단.

    - 오늘 봉은 비교에서 뺀다: 장중 실행이면 임시 종가가 저장돼 있어 기업행위로
      오인되고, 전 종목 대량 재수집을 유발한다 (오늘 봉 자체는 upsert로 갱신됨).
    - 거래량도 비교한다: 액면분할은 거래량도 역비율로 환산되므로, 수정계수가
      1에 가까워 정수 종가가 우연히 같아지는 경우를 거래량이 잡아낸다.
    """
    comparable = [row for row in fetched if row["date"] < today]
    if not comparable:
        return False
    dates = [row["date"] for row in comparable]
    columns = db.stock_candles_1d.c
    with engine.connect() as conn:
        stored = {
            row.date: (row.close, row.volume)
            for row in conn.execute(
                sa.select(columns.date, columns.close, columns.volume).where(
                    columns.symbol == symbol, columns.date.in_(dates)
                )
            )
        }
    return any(
        row["date"] in stored and stored[row["date"]] != (row["close"], row["volume"])
        for row in comparable
    )


def _upsert(engine: Engine, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with engine.begin() as conn:
        db.upsert_rows(conn, db.stock_candles_1d, rows, key_cols=("symbol", "date"))


def _replace_symbol(engine: Engine, symbol: str, rows: list[dict[str, Any]]) -> None:
    """한 트랜잭션 안에서 종목 이력을 통째로 교체한다 (원자적 swap)."""
    with engine.begin() as conn:
        conn.execute(sa.delete(db.stock_candles_1d).where(db.stock_candles_1d.c.symbol == symbol))
        db.upsert_rows(conn, db.stock_candles_1d, rows, key_cols=("symbol", "date"))

"""분봉 수집기 테스트 (`data/minutes.py`).

네트워크 없음 — `MinuteCandleSource` 프로토콜을 스텁으로 대체한다. 가장
중요하게 보는 것: (1) 페이징이 날짜 경계를 넘어 이어진다, (2) 같은 페이지가
되돌아와도 무한 루프에 빠지지 않는다, (3) 누적 거래대금이 봉별 값으로
차분된다, (4) 재실행해도 결과가 같다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from sontrader.client import KisError
from sontrader.data import db
from sontrader.data.minutes import (
    MAX_PAGES_PER_SYMBOL,
    MinuteCollectionAborted,
    collect_minutes,
    collect_minutes_all,
)

NOW = datetime(2026, 8, 24, 15, 30)


def bar(ts: datetime, close: int, *, vol: int = 100, acml: int | None = None) -> dict:
    """API 응답 한 행. 실제 필드명·형식(문자열)을 그대로 흉내낸다."""
    return {
        "stck_bsop_date": ts.strftime("%Y%m%d"),
        "stck_cntg_hour": ts.strftime("%H%M%S"),
        "stck_oprc": str(close),
        "stck_hgpr": str(close + 100),
        "stck_lwpr": str(close - 100),
        "stck_prpr": str(close),
        "cntg_vol": str(vol),
        "acml_tr_pbmn": str(acml if acml is not None else vol * close),
    }


def descending(start: datetime, count: int, *, close: int = 10_000) -> list[dict]:
    """최신이 먼저인 내림차순 — API 응답과 같은 순서."""
    return [bar(start - timedelta(minutes=i), close) for i in range(count)]


class StubSource:
    """요청 기준 시각마다 정해진 페이지를 돌려준다."""

    def __init__(self, pages: dict[datetime, list[dict]] | None = None, default=None):
        self.pages = pages or {}
        self.default = default if default is not None else []
        self.calls: list[datetime] = []

    def get_intraday_candles(self, code: str, reference: datetime) -> list[dict]:
        self.calls.append(reference)
        return self.pages.get(reference, self.default)


def rows_in(engine) -> list[tuple]:
    columns = db.stock_candles_1m.c
    with engine.connect() as conn:
        return conn.execute(
            sa.select(
                columns.symbol, columns.ts, columns.close, columns.volume, columns.trade_value
            ).order_by(columns.ts)
        ).fetchall()


# --- 기본 ---------------------------------------------------------------------


def test_single_page_is_stored_ascending(db_engine):
    db.migrate(db_engine)
    source = StubSource({NOW: descending(NOW, 3)})

    result = collect_minutes(db_engine, source, "005930", now=NOW, days=1)

    stored = rows_in(db_engine)
    # 페이지의 최고(最古) 봉은 거래대금 차분이 불가능해 저장하지 않는다.
    assert [r.ts for r in stored] == [NOW - timedelta(minutes=1), NOW]
    assert result.rows == 2
    assert result.newest == NOW


def test_rerun_is_idempotent(db_engine):
    db.migrate(db_engine)
    source = StubSource({NOW: descending(NOW, 3)})

    collect_minutes(db_engine, source, "005930", now=NOW, days=1)
    collect_minutes(db_engine, source, "005930", now=NOW, days=1)

    assert len(rows_in(db_engine)) == 2  # upsert — 중복 행이 생기지 않는다


# --- 페이징 -------------------------------------------------------------------


def test_paging_follows_the_oldest_bar_of_each_page(db_engine):
    """다음 요청 기준은 이번 페이지의 가장 오래된 봉이다. 그 봉 하나가
    겹치지만 upsert가 흡수한다."""
    db.migrate(db_engine)
    first_oldest = NOW - timedelta(minutes=2)
    source = StubSource(
        {
            NOW: descending(NOW, 3),
            first_oldest: descending(first_oldest, 3),
        }
    )

    collect_minutes(db_engine, source, "005930", now=NOW, days=1)

    assert source.calls[:2] == [NOW, first_oldest]
    # 각 페이지에서 최고 봉이 빠지고, 그 봉은 다음 페이지에서 회복된다.
    # page1 → NOW-1, NOW / page2 → NOW-3, NOW-2  = 4행
    assert [r.ts for r in rows_in(db_engine)] == [
        NOW - timedelta(minutes=3),
        NOW - timedelta(minutes=2),
        NOW - timedelta(minutes=1),
        NOW,
    ]


def test_paging_crosses_the_day_boundary(db_engine):
    """API가 스스로 날짜를 넘겨 채워 준다(실측). 레거시 수집기의 '120건 미만
    이면 전날 자정으로 점프' 휴리스틱을 쓰지 않는 이유."""
    db.migrate(db_engine)
    prev_close = datetime(2026, 8, 21, 15, 30)
    page = [bar(NOW - timedelta(minutes=i), 10_000) for i in range(2)] + [
        bar(prev_close - timedelta(minutes=i), 9_000) for i in range(2)
    ]
    source = StubSource({NOW: page}, default=[])

    collect_minutes(db_engine, source, "005930", now=NOW, days=10)

    days = sorted({r.ts.date() for r in rows_in(db_engine)})
    assert days == [prev_close.date(), NOW.date()]


def test_repeated_page_does_not_loop_forever(db_engine):
    """같은 페이지가 되돌아오면(가장 오래된 봉이 기준보다 늦거나 같으면)
    기준을 전날 자정으로 물린다. 가드가 없으면 영원히 돈다."""
    db.migrate(db_engine)
    # 기준을 뭘 주든 같은 한 건만 돌려주는 소스
    source = StubSource(default=[bar(NOW, 10_000)])

    result = collect_minutes(db_engine, source, "005930", now=NOW, days=3)

    assert result.pages < MAX_PAGES_PER_SYMBOL, "가드가 동작해 상한 전에 끝나야 한다"
    # 전날 자정으로 물리면서 하한(days=3)을 지나 종료된다
    assert len(source.calls) <= 6


def test_page_limit_stops_a_runaway_symbol(db_engine):
    """API가 예상과 다르게 굴 때 한 종목이 유량을 다 태우지 않게 한다."""
    db.migrate(db_engine)

    class EverReceding:
        """항상 기준보다 1분 이른 봉 하나만 준다 — 끝이 없다."""

        def __init__(self):
            self.count = 0

        def get_intraday_candles(self, code, reference):
            self.count += 1
            return [bar(reference - timedelta(minutes=1), 10_000)]

    source = EverReceding()
    result = collect_minutes(db_engine, source, "005930", now=NOW, days=365)

    assert result.pages == MAX_PAGES_PER_SYMBOL


def test_empty_response_ends_collection(db_engine):
    """보관 경계(1년)에 닿으면 빈 응답이 온다(실측: 550일 전 0건)."""
    db.migrate(db_engine)
    oldest = NOW - timedelta(minutes=2)
    source = StubSource({NOW: descending(NOW, 3), oldest: []})

    result = collect_minutes(db_engine, source, "005930", now=NOW, days=365)

    assert result.pages == 2
    assert len(rows_in(db_engine)) == 2  # 최고 봉 제외


# --- 거래대금 -----------------------------------------------------------------


def test_cumulative_trade_value_is_differenced(db_engine):
    """`acml_tr_pbmn`은 그날 시작부터의 누적값이다. 봉별 값은 연속 차분."""
    db.migrate(db_engine)
    t0 = datetime(2026, 8, 24, 9, 0)
    page = [
        bar(t0 + timedelta(minutes=2), 10_000, acml=300),
        bar(t0 + timedelta(minutes=1), 10_000, acml=250),
        bar(t0, 10_000, acml=100),
    ]
    source = StubSource({NOW: page})

    collect_minutes(db_engine, source, "005930", now=NOW, days=1)

    # 09:00은 페이지의 최고 봉이라 빠진다. 남은 두 봉은 차분값.
    values = [r.trade_value for r in rows_in(db_engine)]
    assert values == [150, 50]


def test_trade_value_resets_on_a_new_day(db_engine):
    """날짜마다 리셋된다(실측: 08-21 15:30 7.68조 → 08-24 09:00 2,298억).
    날짜 경계에서 차분하면 음수가 나온다."""
    db.migrate(db_engine)
    page = [
        bar(datetime(2026, 8, 24, 9, 1), 10_000, acml=250),
        bar(datetime(2026, 8, 24, 9, 0), 10_000, acml=100),
        bar(datetime(2026, 8, 21, 15, 30), 9_000, acml=9_999_999),
        # 페이지의 최고 봉은 저장되지 않으므로, 검증 대상이 빠지지 않게 하나 더 둔다
        bar(datetime(2026, 8, 21, 15, 29), 9_000, acml=9_000_000),
    ]
    source = StubSource({NOW: page}, default=[])

    collect_minutes(db_engine, source, "005930", now=NOW, days=10)

    by_ts = {r.ts: r.trade_value for r in rows_in(db_engine)}
    assert by_ts[datetime(2026, 8, 21, 15, 30)] == 999_999  # 같은 날이라 차분
    assert by_ts[datetime(2026, 8, 24, 9, 0)] == 100  # 날짜가 바뀌어 누적값 그대로
    assert by_ts[datetime(2026, 8, 24, 9, 1)] == 150  # 같은 날이라 차분


# --- 결손·이상 응답 ------------------------------------------------------------


def test_rows_with_missing_fields_are_skipped(db_engine):
    """있는 만큼만 쓴다 — 일봉 수집기와 같은 원칙."""
    db.migrate(db_engine)
    good = bar(NOW, 10_000)
    broken = dict(bar(NOW - timedelta(minutes=1), 10_000))
    del broken["stck_hgpr"]
    source = StubSource({NOW: [good, broken]})

    collect_minutes(db_engine, source, "005930", now=NOW, days=1)

    assert [r.ts for r in rows_in(db_engine)] == [NOW]


def test_bars_older_than_the_floor_are_not_stored(db_engine):
    """`days`로 지정한 하한 밖은 버린다."""
    db.migrate(db_engine)
    page = [bar(NOW, 10_000), bar(NOW - timedelta(days=40), 9_000)]
    source = StubSource({NOW: page}, default=[])

    collect_minutes(db_engine, source, "005930", now=NOW, days=7)

    assert [r.ts for r in rows_in(db_engine)] == [NOW]


# --- 증분 --------------------------------------------------------------------


def test_incremental_stops_at_stored_data(db_engine):
    """이미 저장된 구간으로 들어가면 멈춘다 — 1년치를 매번 다시 받지 않는다."""
    db.migrate(db_engine)
    old = NOW - timedelta(minutes=10)
    first = StubSource({NOW: descending(NOW, 3)})
    collect_minutes(db_engine, first, "005930", now=NOW, days=365)

    later = NOW + timedelta(minutes=2)
    second = StubSource({later: descending(later, 3)}, default=descending(old, 3))
    collect_minutes(db_engine, second, "005930", now=later, days=365)

    # 저장분(NOW)에 도달하면 더 파고들지 않는다
    assert len(second.calls) <= 2


# --- 여러 종목 ----------------------------------------------------------------


def test_one_symbol_failure_does_not_stop_the_rest(db_engine):
    db.migrate(db_engine)

    class Flaky:
        def get_intraday_candles(self, code, reference):
            if code == "000660":
                raise KisError("40310000: 일시 오류가 아닌 실패")
            return descending(NOW, 2)

    results, failures = collect_minutes_all(
        db_engine, Flaky(), ["005930", "000660", "035720"], now=NOW, days=1
    )

    assert [r.symbol for r in results] == ["005930", "035720"]
    assert [s for s, _ in failures] == ["000660"]


def test_consecutive_failures_abort_the_run(db_engine):
    """앱키 무효처럼 공통 원인으로 전부 실패하는 상황에서 남은 종목을 계속
    시도하면 시간과 API 유량만 태운다."""
    db.migrate(db_engine)

    class AlwaysFails:
        def get_intraday_candles(self, code, reference):
            raise KisError("EGW00103: 유효하지 않은 AppKey입니다")

    symbols = [f"{i:06d}" for i in range(30)]
    with pytest.raises(MinuteCollectionAborted) as caught:
        collect_minutes_all(db_engine, AlwaysFails(), symbols, now=NOW, days=1)

    assert len(caught.value.failures) == 10  # MAX_CONSECUTIVE_FAILURES


def test_page_oldest_bar_is_not_stored_with_a_cumulative_trade_value(db_engine):
    """실제로 겪은 사고의 회귀 테스트.

    페이지의 가장 오래된 봉은 직전 봉이 그 페이지에 없어 거래대금 차분이
    불가능하고, 누적값이 그대로 들어간다. 연속 페이징 중에는 다음 페이지가
    덮어써서 안 보였고 **증분 수집으로 한 페이지에서 멈출 때만** 남았다 —
    005930 13:21 봉이 정상값의 714배(6,023,125,794,250)로 저장됐다.
    """
    db.migrate(db_engine)
    t0 = datetime(2026, 8, 24, 9, 0)
    # 09:00부터 오름차순 누적: 100, 250, 300 (봉별로는 100, 150, 50)
    page = [
        bar(t0 + timedelta(minutes=2), 10_000, vol=5, acml=300),
        bar(t0 + timedelta(minutes=1), 10_000, vol=15, acml=250),
        bar(t0, 10_000, vol=10, acml=100),
    ]
    # 한 페이지만 주고 그 뒤로는 빈 응답 → 증분 종료와 같은 상황
    source = StubSource({NOW: page}, default=[])

    collect_minutes(db_engine, source, "005930", now=NOW, days=1)

    stored = {r.ts: r.trade_value for r in rows_in(db_engine)}
    assert t0 not in stored, "페이지의 최고(最古) 봉은 저장하지 않는다"
    assert stored[t0 + timedelta(minutes=1)] == 150
    assert stored[t0 + timedelta(minutes=2)] == 50


def test_dropped_oldest_bar_is_recovered_by_the_next_page(db_engine):
    """뺀 봉은 겹침 덕분에 다음 페이지에서 올바른 값으로 저장된다."""
    db.migrate(db_engine)
    t0 = datetime(2026, 8, 24, 9, 0)
    page1 = [
        bar(t0 + timedelta(minutes=3), 10_000, acml=400),
        bar(t0 + timedelta(minutes=2), 10_000, acml=300),
    ]
    # 다음 요청 기준은 page1의 최고 봉(09:02) — 응답에 그 봉이 포함된다
    page2 = [
        bar(t0 + timedelta(minutes=2), 10_000, acml=300),
        bar(t0 + timedelta(minutes=1), 10_000, acml=250),
    ]
    source = StubSource({NOW: page1, t0 + timedelta(minutes=2): page2}, default=[])

    collect_minutes(db_engine, source, "005930", now=NOW, days=1)

    stored = {r.ts: r.trade_value for r in rows_in(db_engine)}
    # 09:02는 page1에서 빠졌지만 page2에서 직전 봉(09:01)과 함께 와 차분됨
    assert stored[t0 + timedelta(minutes=2)] == 50
    assert stored[t0 + timedelta(minutes=3)] == 100

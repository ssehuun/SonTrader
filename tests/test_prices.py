"""일봉 수집기 테스트 — 가짜 시세 소스 + SQLite로 증분/자가치유 로직 검증."""

from datetime import date, timedelta

import sqlalchemy as sa

from sontrader.data import db, prices

TODAY = date(2026, 8, 3)


def raw_row(day: date, close: int, volume: int = 1000):
    return {
        "stck_bsop_date": day.strftime("%Y%m%d"),
        "stck_oprc": str(close - 100),
        "stck_hgpr": str(close + 200),
        "stck_lwpr": str(close - 200),
        "stck_clpr": str(close),
        "acml_vol": str(volume),
        "acml_tr_pbmn": str(close * volume),
        "flng_cls_code": "00",
        "prtt_rate": "0.00",
        "mod_yn": "N",
    }


class FakeClient:
    """날짜→행 사전을 서빙하고 호출 구간을 기록하는 시세 소스."""

    def __init__(self, rows_by_date):
        self.rows_by_date = dict(rows_by_date)
        self.calls: list[tuple[date, date]] = []

    def get_daily_candles(self, code, start, end, adjusted=True):
        assert adjusted is True
        self.calls.append((start, end))
        return [row for day, row in sorted(self.rows_by_date.items()) if start <= day <= end]


def business_days(end: date, count: int) -> list[date]:
    days, cursor = [], end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


def stored_closes(engine, symbol="005930"):
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(db.stock_candles_1d.c.date, db.stock_candles_1d.c.close)
            .where(db.stock_candles_1d.c.symbol == symbol)
            .order_by(db.stock_candles_1d.c.date)
        ).all()
    return {d: c for d, c in rows}


def test_initial_collect_fetches_full_lookback(db_engine):
    db.migrate(db_engine)
    days = business_days(TODAY, 10)
    client = FakeClient({d: raw_row(d, 70000 + i) for i, d in enumerate(days)})

    result = prices.collect_daily(db_engine, client, "005930", today=TODAY, lookback_days=420)

    assert result.full is True
    assert result.rows == 10
    assert client.calls[0][0] == TODAY - timedelta(days=420)
    # 420일 조회가 100일 창 5개로 나뉜다
    assert len(client.calls) == 5
    assert len(stored_closes(db_engine)) == 10


def test_incremental_collect_only_overlap_window(db_engine):
    db.migrate(db_engine)
    days = business_days(TODAY - timedelta(days=3), 10)
    client = FakeClient({d: raw_row(d, 70000) for d in days})
    prices.collect_daily(db_engine, client, "005930", today=TODAY - timedelta(days=3))

    new_day = TODAY - timedelta(days=1)
    client.rows_by_date[new_day] = raw_row(new_day, 71000)
    client.calls.clear()

    result = prices.collect_daily(db_engine, client, "005930", today=TODAY)

    assert result.full is False
    last_stored = max(days)
    assert client.calls == [(last_stored - timedelta(days=prices.OVERLAP_DAYS), TODAY)]
    assert stored_closes(db_engine)[new_day] == 71000


def test_rerun_is_idempotent(db_engine):
    db.migrate(db_engine)
    days = business_days(TODAY, 5)
    client = FakeClient({d: raw_row(d, 70000) for d in days})

    prices.collect_daily(db_engine, client, "005930", today=TODAY)
    before = stored_closes(db_engine)
    prices.collect_daily(db_engine, client, "005930", today=TODAY)

    assert stored_closes(db_engine) == before


def test_corporate_action_triggers_full_refetch(db_engine):
    # 5:1 액면분할 시나리오: 저장된 과거 종가와 새로 받은 (소급 수정된) 종가가
    # 달라지면 종목 전체를 지우고 다시 받아야 한다.
    db.migrate(db_engine)
    days = business_days(TODAY - timedelta(days=3), 10)
    client = FakeClient({d: raw_row(d, 70000) for d in days})
    prices.collect_daily(db_engine, client, "005930", today=TODAY - timedelta(days=3))

    split_day = TODAY - timedelta(days=1)
    adjusted = {d: raw_row(d, 14000) for d in days}  # 과거 전체가 1/5로 소급 수정
    adjusted[split_day] = raw_row(split_day, 14200)
    client.rows_by_date = adjusted

    result = prices.collect_daily(db_engine, client, "005930", today=TODAY)

    assert result.full is True
    closes = stored_closes(db_engine)
    assert closes[days[0]] == 14000  # 과거분이 수정 기준으로 교체됐다
    assert closes[split_day] == 14200


def test_intraday_provisional_close_is_not_mistaken_for_adjustment(db_engine):
    # 장중 실행으로 저장된 오늘의 임시 종가가 이후 실행에서 달라져도,
    # 기업행위(전체 재수집)가 아니라 단순 upsert 갱신이어야 한다.
    db.migrate(db_engine)
    days = business_days(TODAY, 5)
    intraday = {d: raw_row(d, 70000) for d in days}
    client = FakeClient(intraday)
    prices.collect_daily(db_engine, client, "005930", today=TODAY)

    client.rows_by_date[TODAY] = raw_row(TODAY, 70500)  # 마감 후 확정 종가
    client.calls.clear()

    result = prices.collect_daily(db_engine, client, "005930", today=TODAY)

    assert result.full is False  # 전체 재수집이 아니다
    assert stored_closes(db_engine)[TODAY] == 70500  # upsert로 갱신만 된다


def test_adjustment_refetch_failure_preserves_history(db_engine):
    # 소급 수정 감지 후 전체 재수집이 실패하면, 기존 이력이 지워지면 안 된다.
    db.migrate(db_engine)
    days = business_days(TODAY - timedelta(days=3), 10)
    client = FakeClient({d: raw_row(d, 70000) for d in days})
    prices.collect_daily(db_engine, client, "005930", today=TODAY - timedelta(days=3))
    before = stored_closes(db_engine)

    class AdjustedThenFailing(FakeClient):
        def get_daily_candles(self, code, start, end, adjusted=True):
            if (end - start).days > prices.OVERLAP_DAYS + 5:
                raise RuntimeError("full refetch fails")  # 전체 구간 요청만 실패
            return [raw_row(d, 14000) for d in days if start <= d <= end]  # 소급 수정된 값

    failing = AdjustedThenFailing({})
    import pytest

    with pytest.raises(RuntimeError):
        prices.collect_daily(db_engine, failing, "005930", today=TODAY)

    assert stored_closes(db_engine) == before  # 이력 보존 (원자적 swap)


def test_pace_sleeps_before_every_api_call(db_engine):
    db.migrate(db_engine)
    client = FakeClient({})
    naps: list[float] = []

    prices.collect_daily(
        db_engine,
        client,
        "005930",
        today=TODAY,
        lookback_days=420,
        pace_seconds=0.5,
        sleep=naps.append,
    )

    assert len(naps) == len(client.calls) == 5
    assert set(naps) == {0.5}


def test_transient_server_errors_are_retried(db_engine):
    import httpx

    db.migrate(db_engine)
    day = TODAY - timedelta(days=1)

    class Flaky500Client(FakeClient):
        def __init__(self, rows):
            super().__init__(rows)
            self.attempts = 0

        def get_daily_candles(self, code, start, end, adjusted=True):
            self.attempts += 1
            if self.attempts == 1:
                request = httpx.Request("GET", "https://api")
                response = httpx.Response(500, request=request)
                raise httpx.HTTPStatusError("boom", request=request, response=response)
            return super().get_daily_candles(code, start, end, adjusted)

    client = Flaky500Client({day: raw_row(day, 70000)})
    naps: list[float] = []

    result = prices.collect_daily(
        db_engine, client, "005930", today=TODAY, lookback_days=5, sleep=naps.append
    )

    assert result.rows == 1
    assert client.attempts == 2  # 1회 실패 후 재시도로 성공
    assert 1.0 in naps  # 백오프 수면


def test_collect_all_isolates_per_symbol_failures(db_engine):
    db.migrate(db_engine)

    class FlakyClient(FakeClient):
        def get_daily_candles(self, code, start, end, adjusted=True):
            if code == "999999":
                raise RuntimeError("boom")
            return super().get_daily_candles(code, start, end, adjusted)

    day = TODAY - timedelta(days=1)
    client = FlakyClient({day: raw_row(day, 70000)})

    results, failures = prices.collect_daily_all(
        db_engine, client, ["005930", "999999", "000660"], today=TODAY
    )

    assert [r.symbol for r in results] == ["005930", "000660"]
    assert [symbol for symbol, _ in failures] == ["999999"]


# --- 백필 (과거 방향) --------------------------------------------------------


def seed(engine, days, close=1000):
    """일봉 몇 개를 미리 넣어둔다 — 백필은 여기서 거슬러 올라간다."""
    db.migrate(engine)
    client = FakeClient({d: raw_row(d, close) for d in days})
    prices.collect_daily(engine, client, "005930", today=days[-1], lookback_days=30)
    return client


def test_backfill_fills_older_range_and_stops_at_listing_date(db_engine):
    recent = business_days(TODAY, 5)
    seed(db_engine, recent)

    listed = TODAY - timedelta(days=250)
    older = business_days(recent[0] - timedelta(days=1), 40)
    client = FakeClient({d: raw_row(d, 900) for d in older if d >= listed})

    result = prices.backfill_daily(db_engine, client, "005930", listing_date=listed)

    stored = dict(stored_closes(db_engine))
    assert min(stored) == min(d for d in older if d >= listed)
    assert result.rows > 0
    # 상장일보다 과거는 절대 조회하지 않는다 — 빈 응답만 받아 호출을 낭비한다.
    assert all(start >= listed for start, _ in client.calls)


def test_backfill_never_touches_existing_rows(db_engine):
    recent = business_days(TODAY, 5)
    seed(db_engine, recent, close=1000)
    before = dict(stored_closes(db_engine))

    older = business_days(recent[0] - timedelta(days=1), 10)
    # 겹치는 날짜에 다른 종가를 주더라도, 백필은 과거 구간만 조회하므로 안 닿는다.
    client = FakeClient({d: raw_row(d, 777) for d in older + recent})
    prices.backfill_daily(db_engine, client, "005930", listing_date=older[0])

    after = dict(stored_closes(db_engine))
    assert all(after[d] == before[d] for d in before)
    assert all(end < recent[0] for _, end in client.calls)


def test_backfill_stops_after_consecutive_empty_pages(db_engine):
    """상장일자가 없어도 빈 페이지 연속으로 종료한다 (무한 루프 방지)."""
    recent = business_days(TODAY, 5)
    seed(db_engine, recent)
    client = FakeClient({})  # 과거 데이터가 전혀 없음

    result = prices.backfill_daily(db_engine, client, "005930", listing_date=None)

    assert result.rows == 0
    assert result.pages == prices.BACKFILL_EMPTY_TOLERANCE


def test_backfill_tolerates_a_gap_longer_than_one_page(db_engine):
    """장기 거래정지로 중간 페이지가 비어도 그보다 과거를 포기하지 않는다."""
    recent = business_days(TODAY, 5)
    seed(db_engine, recent)

    # 최근 이전 150일은 공백, 그보다 과거에 데이터가 있다.
    gap_start = recent[0] - timedelta(days=150)
    ancient = business_days(gap_start, 10)
    client = FakeClient({d: raw_row(d, 500) for d in ancient})

    result = prices.backfill_daily(
        db_engine, client, "005930", listing_date=ancient[0] - timedelta(days=5)
    )

    assert result.rows == len(ancient)
    assert min(dict(stored_closes(db_engine))) == ancient[0]


def test_backfill_respects_earliest_floor(db_engine):
    recent = business_days(TODAY, 5)
    seed(db_engine, recent)

    floor = recent[0] - timedelta(days=30)
    older = business_days(recent[0] - timedelta(days=1), 60)
    client = FakeClient({d: raw_row(d, 800) for d in older})

    prices.backfill_daily(db_engine, client, "005930", listing_date=older[0], earliest=floor)

    assert min(dict(stored_closes(db_engine))) >= floor
    assert all(start >= floor for start, _ in client.calls)


def test_backfill_skips_symbols_with_no_data(db_engine):
    """한 봉도 없으면 어디서 거슬러 올라갈지 기준이 없다 — 호출하지 않는다."""
    db.migrate(db_engine)
    client = FakeClient({})

    result = prices.backfill_daily(db_engine, client, "005930", listing_date=TODAY)

    assert result == prices.BackfillResult(symbol="005930", rows=0, pages=0, reached=None)
    assert client.calls == []


def test_backfill_all_isolates_per_symbol_failures(db_engine):
    recent = business_days(TODAY, 5)
    db.migrate(db_engine)
    for symbol in ("005930", "000660"):
        c = FakeClient({d: raw_row(d, 1000) for d in recent})
        prices.collect_daily(db_engine, c, symbol, today=recent[-1], lookback_days=30)

    class Exploding(FakeClient):
        def get_daily_candles(self, code, start, end, adjusted=True):
            if code == "000660":
                raise RuntimeError("boom")
            return super().get_daily_candles(code, start, end, adjusted)

    older = business_days(recent[0] - timedelta(days=1), 10)
    client = Exploding({d: raw_row(d, 900) for d in older})

    results, failures = prices.backfill_daily_all(
        db_engine,
        client,
        ["005930", "000660"],
        listing_dates={"005930": older[0], "000660": older[0]},
    )

    assert [r.symbol for r in results] == ["005930"]
    assert [s for s, _ in failures] == ["000660"]


def test_self_heal_refetches_back_to_the_existing_history_start(db_engine):
    """기업행위 재수집이 backfill로 채운 과거를 날리지 않는다.

    _replace_symbol()은 종목 행을 전부 지우고 새로 받은 것만 넣는다. 재수집
    범위를 lookback_days로만 잡으면 그보다 과거가 조용히 사라진다 — 실제로
    2018년까지 2,116봉 있던 종목이 420일치 282봉으로 줄었다.
    """
    db.migrate(db_engine)
    deep = business_days(TODAY, 200)  # 재수집 기본 범위(30일)보다 훨씬 깊은 이력
    client = FakeClient({d: raw_row(d, 1000) for d in deep})
    prices.collect_daily(db_engine, client, "005930", today=TODAY, lookback_days=400)
    assert len(stored_closes(db_engine)) == len(deep)

    # 다음 수집에서 겹침 구간 종가가 달라진다 = 기업행위 감지
    healed = FakeClient({d: raw_row(d, 500) for d in deep})
    result = prices.collect_daily(db_engine, healed, "005930", today=TODAY, lookback_days=30)

    stored = dict(stored_closes(db_engine))
    assert result.full is True
    assert min(stored) == deep[0]  # 과거가 보존됐다
    assert len(stored) == len(deep)
    assert all(close == 500 for close in stored.values())  # 새 기준으로 전부 교체


def test_self_heal_still_covers_lookback_when_history_is_shallow(db_engine):
    """기존 이력이 lookback_days보다 얕으면 lookback_days까지 넓혀 받는다."""
    db.migrate(db_engine)
    shallow = business_days(TODAY, 5)
    client = FakeClient({d: raw_row(d, 1000) for d in shallow})
    prices.collect_daily(db_engine, client, "005930", today=TODAY, lookback_days=7)

    wide = business_days(TODAY, 40)
    healed = FakeClient({d: raw_row(d, 500) for d in wide})
    prices.collect_daily(db_engine, healed, "005930", today=TODAY, lookback_days=60)

    stored = dict(stored_closes(db_engine))
    assert min(stored) < shallow[0]  # 기존보다 더 과거까지 받았다

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

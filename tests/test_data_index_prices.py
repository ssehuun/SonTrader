"""지수 일봉 수집 테스트 (R22).

네트워크를 쓰지 않는다 — `IndexCandleSource`를 스텁으로 대체한다.

중심은 **페이징**이다. 이 엔드포인트는 요청 구간이 아니라 **건수**(50)가
상한이라, 창을 앞으로 미는 일봉 수집기와 방향이 반대다. 커서가 뒤로
물러나지 않으면 무한 루프가 되므로 그 성질을 촘촘히 본다.
"""

from __future__ import annotations

from datetime import date, timedelta

from sontrader.data import db
from sontrader.data.index_prices import (
    KOSDAQ,
    KOSPI,
    PAGE_SIZE,
    collect_index,
    load_closes,
)

END = date(2026, 8, 27)


def row(day: date, close: float, **over):
    base = {
        "stck_bsop_date": day.strftime("%Y%m%d"),
        "bstp_nmix_prpr": f"{close:.2f}",
        "bstp_nmix_oprc": f"{close - 1:.2f}",
        "bstp_nmix_hgpr": f"{close + 2:.2f}",
        "bstp_nmix_lwpr": f"{close - 3:.2f}",
        "acml_vol": "268465",
        "acml_tr_pbmn": "22943592",
    }
    return {**base, **over}


class StubClient:
    """`history`(날짜→종가)에서 요청 구간에 드는 것 중 **최신 `PAGE_SIZE`건**만
    돌려준다 — 실제 API의 성질을 그대로 흉내낸다."""

    def __init__(self, history: dict[date, float], page_size: int = PAGE_SIZE) -> None:
        self._history = history
        self._page = page_size
        self.calls: list[tuple[date, date]] = []

    def get_index_daily_candles(self, code, start, end):
        self.calls.append((start, end))
        days = sorted(d for d in self._history if start <= d <= end)
        return [row(d, self._history[d]) for d in days[-self._page :]]


def make_history(n: int, *, end: date = END) -> dict[date, float]:
    """`end`부터 과거로 n 거래일 (주말은 무시 — 페이징 논리만 본다)."""
    return {end - timedelta(days=i): 1000.0 + i for i in range(n)}


def test_a_single_page_is_stored(db_engine):
    db.migrate(db_engine)
    client = StubClient(make_history(3))

    result = collect_index(db_engine, client, end=END)

    assert result.rows == 3
    assert result.newest == END
    closes = load_closes(db_engine, KOSPI, END - timedelta(days=10), END)
    assert closes[END] == 1000.0


def test_paging_walks_backwards_because_the_cap_is_a_row_count(db_engine):
    """요청 구간이 아니라 건수가 상한이라, 창을 **과거로 물려** 가며 받는다."""
    db.migrate(db_engine)
    client = StubClient(make_history(120), page_size=50)

    result = collect_index(db_engine, client, end=END)

    assert result.rows == 120
    assert result.newest == END
    assert result.oldest == END - timedelta(days=119)
    # 50건씩이라 세 번 이상 불러야 120건이 된다.
    assert len(client.calls) >= 3
    # 커서가 단조 감소해야 한다 — 아니면 무한 루프다.
    ends = [end for _, end in client.calls]
    assert ends == sorted(ends, reverse=True)
    assert len(set(ends)) == len(ends)


def test_collection_stops_at_the_earliest_bound(db_engine):
    db.migrate(db_engine)
    client = StubClient(make_history(200))
    floor = END - timedelta(days=60)

    result = collect_index(db_engine, client, end=END, earliest=floor)

    assert result.oldest >= floor
    stored = load_closes(db_engine, KOSPI, floor - timedelta(days=30), END)
    assert min(stored) >= floor


def test_an_empty_response_ends_collection(db_engine):
    """보관 경계를 상수로 박지 않는다 — 빈 응답이 끝을 알린다."""
    db.migrate(db_engine)
    client = StubClient(make_history(10))

    result = collect_index(db_engine, client, end=END)

    assert result.rows == 10
    # 마지막 호출은 데이터가 없는 구간이라 빈 응답을 받고 멈춘다.
    assert client.calls[-1][1] < result.oldest


def test_rerunning_does_not_duplicate(db_engine):
    """upsert라 재실행해도 결과가 같다."""
    db.migrate(db_engine)
    client = StubClient(make_history(30))

    collect_index(db_engine, client, end=END)
    collect_index(db_engine, client, end=END)

    assert len(load_closes(db_engine, KOSPI, END - timedelta(days=60), END)) == 30


def test_decimal_index_values_survive_the_round_trip(db_engine):
    """지수는 소수 2자리다(6912.37). 정수로 캐스팅하면 조용히 뭉개진다 —
    `stock_candles_1d`가 Integer라 이 테이블이 따로 있는 이유다."""
    db.migrate(db_engine)
    client = StubClient({END: 6912.37})

    collect_index(db_engine, client, end=END)

    assert load_closes(db_engine, KOSPI, END, END)[END] == 6912.37


def test_rows_without_a_close_are_dropped(db_engine):
    """지수 종가는 G3 판정의 유일한 입력이라, 없는 채로 넣으면 그 날짜만
    조용히 비교에서 빠진다."""
    db.migrate(db_engine)

    class Broken:
        def get_index_daily_candles(self, code, start, end):
            if end < END:
                return []
            return [row(END, 100.0), {"stck_bsop_date": "20260826"}]  # 종가 없음

    collect_index(db_engine, Broken(), end=END)

    assert list(load_closes(db_engine, KOSPI, END - timedelta(days=5), END)) == [END]


def test_kospi_and_kosdaq_are_stored_separately(db_engine):
    """`code`가 PK의 일부다 — 두 지수가 같은 날짜에 공존해야 한다."""
    db.migrate(db_engine)

    collect_index(db_engine, StubClient({END: 6912.37}), KOSPI, end=END)
    collect_index(db_engine, StubClient({END: 900.12}), KOSDAQ, end=END)

    assert load_closes(db_engine, KOSPI, END, END)[END] == 6912.37
    assert load_closes(db_engine, KOSDAQ, END, END)[END] == 900.12


def test_pacing_is_applied_between_pages_not_before_the_first(db_engine):
    db.migrate(db_engine)
    client = StubClient(make_history(120), page_size=50)
    slept: list[float] = []

    collect_index(db_engine, client, end=END, pace_seconds=0.2, sleep=slept.append)

    assert slept == [0.2] * (len(client.calls) - 1)


def test_a_client_that_never_advances_does_not_loop_forever(db_engine):
    """같은 페이지가 계속 돌아오면 커서가 진행하지 않는다 — 끊어야 한다."""
    db.migrate(db_engine)

    class Stuck:
        calls = 0

        def get_index_daily_candles(self, code, start, end):
            Stuck.calls += 1
            return [row(END, 100.0)]  # 언제 요청하든 같은 날짜만 준다

    result = collect_index(db_engine, Stuck(), end=END)

    assert Stuck.calls < 10  # 상한(200)까지 돌지 않고 커서 가드가 먼저 끊는다
    assert result.rows >= 1

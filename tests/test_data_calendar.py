"""data/calendar.py 테스트."""

from dataclasses import replace
from datetime import date

import httpx

from sontrader.client import KisClient
from sontrader.data import calendar, db
from tests.conftest import TOKEN_RESPONSE


def make_client(settings, responder) -> KisClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(200, json=TOKEN_RESPONSE)
        return responder(request)

    real_settings = replace(settings, paper=False)  # CTCA0903R는 실전 전용
    return KisClient(real_settings, transport=httpx.MockTransport(handler))


def holiday_response(rows: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"rt_cd": "0", "output": rows})


def row(bass_dt: str, *, opnd_yn: str = "Y") -> dict:
    return {
        "bass_dt": bass_dt,
        "wday_dvsn_cd": "03",
        "bzdy_yn": "Y",
        "tr_day_yn": "Y",
        "opnd_yn": opnd_yn,
        "sttl_day_yn": "Y",
    }


def test_is_open_returns_none_when_nothing_cached(db_engine):
    db.migrate(db_engine)
    assert calendar.is_open(db_engine, date(2026, 3, 10)) is None


def test_refresh_if_needed_fetches_and_stores_when_not_cached(db_engine, settings):
    db.migrate(db_engine)
    calls = []

    def responder(request):
        calls.append(request)
        assert request.url.params["BASS_DT"] == "20260310"
        return holiday_response([row("20260310", opnd_yn="Y"), row("20260311", opnd_yn="N")])

    client = make_client(settings, responder)
    calendar.refresh_if_needed(db_engine, client, today=date(2026, 3, 10))

    assert len(calls) == 1
    assert calendar.is_open(db_engine, date(2026, 3, 10)) is True
    assert calendar.is_open(db_engine, date(2026, 3, 11)) is False


def test_refresh_if_needed_skips_call_when_today_already_cached(db_engine, settings):
    db.migrate(db_engine)
    calls = []

    def responder(request):
        calls.append(request)
        return holiday_response([row("20260310", opnd_yn="Y")])

    client = make_client(settings, responder)
    calendar.refresh_if_needed(db_engine, client, today=date(2026, 3, 10))
    calendar.refresh_if_needed(db_engine, client, today=date(2026, 3, 10))  # 재호출 안 됨

    assert len(calls) == 1


def test_refresh_if_needed_fetches_again_once_cache_no_longer_covers_today(db_engine, settings):
    db.migrate(db_engine)
    calls = []

    def responder(request):
        calls.append(request)
        return holiday_response([row(request.url.params["BASS_DT"])])

    client = make_client(settings, responder)
    calendar.refresh_if_needed(db_engine, client, today=date(2026, 3, 10))
    calendar.refresh_if_needed(db_engine, client, today=date(2026, 4, 1))  # 캐시에 없는 날짜

    assert len(calls) == 2


def test_store_upserts_existing_dates(db_engine, settings):
    db.migrate(db_engine)
    responses = iter(
        [
            holiday_response([row("20260310", opnd_yn="Y")]),
            holiday_response([row("20260310", opnd_yn="N")]),
        ]
    )

    client = make_client(settings, lambda request: next(responses))
    calendar._store(db_engine, client.get_market_holidays(date(2026, 3, 10)))
    calendar._store(db_engine, client.get_market_holidays(date(2026, 3, 10)))

    assert calendar.is_open(db_engine, date(2026, 3, 10)) is False

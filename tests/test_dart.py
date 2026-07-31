"""DART collector tests — httpx.MockTransport for the API, SQLite for ingest."""

import json
from datetime import date, datetime

import httpx
import pytest
import sqlalchemy as sa

from sontrader.data import dart, db


def item(
    rcept_no, corp_cls="Y", stock_code="005930", report_nm="주요사항보고서", corp_code="00126380"
):
    return {
        "corp_cls": corp_cls,
        "corp_name": "삼성전자",
        "corp_code": corp_code,
        "stock_code": stock_code,
        "corp_name_eng": "SAMSUNG",
        "report_nm": report_nm,
        "rcept_no": rcept_no,
        "flr_nm": "삼성전자",
        "rcept_dt": "20260731",
        "rm": "유",
    }


def make_client(pages):
    """pages: list of response dicts, served in order of page_no."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "opendart.fss.or.kr"
        page_no = int(request.url.params["page_no"])
        return httpx.Response(200, text=json.dumps(pages[page_no - 1]))

    return dart.DartClient("test-key", transport=httpx.MockTransport(handler))


def page(items, page_no, total_page):
    return {
        "status": "000",
        "message": "정상",
        "page_no": page_no,
        "page_count": 100,
        "total_count": len(items) * total_page,
        "total_page": total_page,
        "list": items,
    }


def test_pagination_follows_all_pages():
    pages = [
        page([item("20260731000001"), item("20260731000002")], 1, 2),
        page([item("20260731000003")], 2, 2),
    ]
    with make_client(pages) as client:
        disclosures = client.list_disclosures(date(2026, 7, 31))

    assert [d.rcept_no for d in disclosures] == [
        "20260731000001",
        "20260731000002",
        "20260731000003",
    ]


def test_only_watched_markets_are_kept():
    pages = [
        page(
            [
                item("20260731000001", corp_cls="Y"),
                item("20260731000002", corp_cls="K"),
                item("20260731000003", corp_cls="N"),  # 코넥스 제외
                item("20260731000004", corp_cls="E"),  # 기타 제외
            ],
            1,
            1,
        )
    ]
    with make_client(pages) as client:
        disclosures = client.list_disclosures(date(2026, 7, 31))

    assert {d.rcept_no for d in disclosures} == {"20260731000001", "20260731000002"}


def test_no_data_status_returns_empty():
    pages = [{"status": "013", "message": "조회된 데이타가 없습니다."}]
    with make_client(pages) as client:
        assert client.list_disclosures(date(2026, 7, 26)) == []


def test_error_status_raises():
    pages = [{"status": "020", "message": "요청 제한을 초과하였습니다."}]
    with make_client(pages) as client:
        with pytest.raises(dart.DartError, match="020"):
            client.list_disclosures(date(2026, 7, 31))


def test_blank_stock_code_becomes_none():
    pages = [page([item("20260731000001", stock_code=" ")], 1, 1)]
    with make_client(pages) as client:
        (d,) = client.list_disclosures(date(2026, 7, 31))

    assert d.stock_code is None


def test_null_fields_do_not_crash_parsing():
    # OpenDART는 키 부재가 아니라 JSON null을 줄 수 있다.
    broken = item("20260731000001")
    broken["stock_code"] = None
    broken["rcept_dt"] = None  # 접수번호 앞 8자리로 복원되어야 한다
    unrecoverable = {"rcept_no": None, "corp_cls": "Y"}  # 행 단위로 버려져야 한다
    pages = [page([broken, unrecoverable, item("20260731000002")], 1, 1)]

    with make_client(pages) as client:
        disclosures = client.list_disclosures(date(2026, 7, 31))

    assert [d.rcept_no for d in disclosures] == ["20260731000001", "20260731000002"]
    assert disclosures[0].stock_code is None
    assert disclosures[0].rcept_dt == date(2026, 7, 31)


def test_non_json_body_raises_dart_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>점검 중</html>")

    with dart.DartClient("test-key", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(dart.DartError):
            client.list_disclosures(date(2026, 7, 31))


def test_fatal_statuses_are_distinguishable():
    pages = [{"status": "010", "message": "등록되지 않은 키입니다."}]
    with make_client(pages) as client:
        with pytest.raises(dart.DartError) as excinfo:
            client.list_disclosures(date(2026, 7, 31))

    assert excinfo.value.status in dart.FATAL_STATUSES


def test_normalization_maps_refiling_to_same_key():
    original = dart.Disclosure(
        rcept_no="20260731000001",
        corp_code="00126380",
        corp_name="삼성전자",
        stock_code="005930",
        corp_cls="Y",
        report_nm="연결재무제표기준영업(잠정)실적(공정공시)",
        rcept_dt=date(2026, 7, 31),
    )
    refiled = dart.Disclosure(
        rcept_no="20260801000042",
        corp_code="00126380",
        corp_name="삼성전자",
        stock_code="005930",
        corp_cls="Y",
        report_nm="[기재정정] 연결재무제표기준영업(잠정)실적 (공정공시)",
        rcept_dt=date(2026, 8, 1),
    )

    assert dart.norm_key(original) == dart.norm_key(refiled)


def test_non_correction_bracket_tags_are_distinct_events():
    # "[발행조건확정]"은 정정이 아니라 별개 이벤트 — norm_key가 달라야 한다.
    assert dart.normalize_title("[발행조건확정]증권신고서(채무증권)") != dart.normalize_title(
        "증권신고서(채무증권)"
    )


def test_same_title_in_different_quarters_is_distinct():
    # 같은 회사의 동일 제목 반복 공시(공급계약 등)는 분기가 다르면 별개 이벤트다.
    march = dart.Disclosure(
        rcept_no="20260310000001",
        corp_code="00126380",
        corp_name="삼성전자",
        stock_code="005930",
        corp_cls="Y",
        report_nm="단일판매ㆍ공급계약체결",
        rcept_dt=date(2026, 3, 10),
    )
    september = dart.Disclosure(
        rcept_no="20260910000001",
        corp_code="00126380",
        corp_name="삼성전자",
        stock_code="005930",
        corp_cls="Y",
        report_nm="단일판매ㆍ공급계약체결",
        rcept_dt=date(2026, 9, 10),
    )

    assert dart.norm_key(march) != dart.norm_key(september)


def test_classify_event_types():
    assert dart.classify("연결재무제표기준영업(잠정)실적(공정공시)") == "earnings"
    assert dart.classify("[기재정정]유상증자결정") == "capital_change"
    assert dart.classify("단일판매ㆍ공급계약체결") == "supply_contract"
    assert dart.classify("주식등의대량보유상황보고서") == "other"
    # 주식분할·액면분할은 기업분할(mna)로 오분류되면 안 된다.
    assert dart.classify("주식분할결정") == "capital_change"
    assert dart.classify("회사분할결정") == "mna"
    # 자기주식 취득(매수 신호)과 처분(매도 신호)은 구분되어야 한다.
    assert dart.classify("자기주식취득결정") == "buyback"
    assert dart.classify("자기주식처분결정") == "share_disposal"


def test_ingest_is_idempotent_and_records_dual_timestamps(db_engine):
    db.migrate(db_engine)
    pages = [page([item("20260731000001"), item("20260731000002")], 1, 1)]
    with make_client(pages) as client:
        disclosures = client.list_disclosures(date(2026, 7, 31))

    first = dart.ingest(db_engine, disclosures, ingested_at=datetime(2026, 7, 31, 16, 1, 0))
    second = dart.ingest(db_engine, disclosures, ingested_at=datetime(2026, 7, 31, 16, 6, 0))

    assert (first, second) == (2, 0)
    with db_engine.connect() as conn:
        rows = conn.execute(
            sa.select(db.events.c.event_id, db.events.c.published_at, db.events.c.ingested_at)
        ).all()
    assert len(rows) == 2
    for _, published_at, ingested_at in rows:
        assert published_at == datetime(2026, 7, 31, 0, 0, 0)  # 접수일
        assert ingested_at == datetime(2026, 7, 31, 16, 1, 0)  # 최초 수집 시각 유지


def test_ingest_stores_symbol_and_norm_key(db_engine):
    db.migrate(db_engine)
    disclosure = dart.Disclosure(
        rcept_no="20260731000001",
        corp_code="00126380",
        corp_name="삼성전자",
        stock_code="005930",
        corp_cls="Y",
        report_nm="[첨부정정]연결재무제표기준영업(잠정)실적",
        rcept_dt=date(2026, 7, 31),
    )

    dart.ingest(db_engine, [disclosure], ingested_at=datetime(2026, 7, 31, 16, 1, 0))

    with db_engine.connect() as conn:
        row = conn.execute(sa.select(db.events)).mappings().one()
    assert row["symbol"] == "005930"
    assert row["event_type"] == "earnings"
    assert row["norm_key"] == "00126380:2026Q3:연결재무제표기준영업(잠정)실적"
    assert row["raw_json"]["corp_name"] == "삼성전자"

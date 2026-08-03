"""종목 마스터 파싱/적재 테스트 — 합성 .mst 라인과 인메모리 zip 사용."""

import io
import zipfile
from datetime import datetime

import httpx
import sqlalchemy as sa

from sontrader.data import db, master


def build_line(market, symbol, name, **fields):
    """모듈의 폭 명세로부터 합성 마스터 행을 만든다 (폭 불일치 방지)."""
    slices, tail_width = master._SPECS[market]
    tail = [" "] * tail_width
    for field_name, value in fields.items():
        sl = slices[field_name]
        width = sl.stop - sl.start
        tail[sl] = list(str(value)[:width].rjust(width))
    return f"{symbol:<9}" + f"{'KR' + symbol:<12}" + name + "".join(tail)


SAMSUNG_FIELDS = dict(
    그룹코드="ST",
    시가총액규모="1",
    저유동성="N",
    SPAC="N",
    우선주="0",
    기준가="71000",
    거래정지="N",
    정리매매="N",
    관리종목="N",
    전일거래량="12345678",
    영업이익="651977",
    시가총액="4200000",
)


def test_parse_kospi_master_extracts_fields():
    text = build_line("KOSPI", "005930", "삼성전자", **SAMSUNG_FIELDS)
    (row,) = master.parse_master(text, "KOSPI")

    assert row.symbol == "005930"
    assert row.name == "삼성전자"
    assert row.market == "KOSPI"
    assert row.group_code == "ST"
    assert row.base_price == 71000
    assert row.suspended_yn == "N"
    assert row.prev_volume == 12345678
    assert row.market_cap == 4200000


def test_parse_kosdaq_master_uses_same_field_names():
    text = build_line(
        "KOSDAQ",
        "247540",
        "에코프로비엠",
        그룹코드="ST",
        기준가="105000",
        관리종목="N",
        SPAC="N",
        영업이익="-1500",  # 적자 기업도 파싱되어야 한다
    )
    (row,) = master.parse_master(text, "KOSDAQ")

    assert row.symbol == "247540"
    assert row.market == "KOSDAQ"
    assert row.base_price == 105000
    assert row.op_profit == -1500


def test_parse_skips_malformed_lines():
    good = build_line("KOSPI", "005930", "삼성전자", **SAMSUNG_FIELDS)
    text = "짧은 불량 행\n" + good + "\n"

    rows = master.parse_master(text, "KOSPI")

    assert [r.symbol for r in rows] == ["005930"]


def test_fetch_master_downloads_and_parses_zip():
    text = build_line("KOSPI", "005930", "삼성전자", **SAMSUNG_FIELDS)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("kospi_code.mst", text.encode("cp949"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == master.MASTER_URLS["KOSPI"]
        return httpx.Response(200, content=buffer.getvalue())

    rows = master.fetch_master("KOSPI", transport=httpx.MockTransport(handler))

    assert rows[0].symbol == "005930"
    assert rows[0].name == "삼성전자"


def test_upsert_master_is_idempotent_and_updates(db_engine):
    db.migrate(db_engine)
    (row,) = master.parse_master(
        build_line("KOSPI", "005930", "삼성전자", **SAMSUNG_FIELDS), "KOSPI"
    )

    master.upsert_master(db_engine, [row], updated_at=datetime(2026, 8, 3, 8, 0, 0))
    count, removed = master.upsert_master(
        db_engine,
        [master.MasterRow(**{**row.__dict__, "name": "삼성전자우아님"})],
        updated_at=datetime(2026, 8, 4, 8, 0, 0),
    )

    with db_engine.connect() as conn:
        stored = conn.execute(sa.select(db.symbol_master)).mappings().all()
    assert (count, removed) == (1, 0)
    assert len(stored) == 1
    assert stored[0]["name"] == "삼성전자우아님"
    assert stored[0]["updated_at"] == datetime(2026, 8, 4, 8, 0, 0)


def test_upsert_master_prunes_delisted_symbols_of_same_market(db_engine):
    db.migrate(db_engine)
    parse = lambda sym, name: master.parse_master(  # noqa: E731
        build_line("KOSPI", sym, name, **SAMSUNG_FIELDS), "KOSPI"
    )[0]
    kosdaq_fields = {**SAMSUNG_FIELDS}
    kosdaq_row = master.parse_master(
        build_line("KOSDAQ", "247540", "에코프로비엠", **kosdaq_fields), "KOSDAQ"
    )[0]

    master.upsert_master(
        db_engine,
        [parse("005930", "삼성전자"), parse("000660", "SK하이닉스")],
        datetime(2026, 8, 3),
    )
    master.upsert_master(db_engine, [kosdaq_row], datetime(2026, 8, 3))
    # 다음 날 KOSPI 파일에서 SK하이닉스가 사라졌다 (상장폐지 가정).
    count, removed = master.upsert_master(
        db_engine, [parse("005930", "삼성전자")], datetime(2026, 8, 4)
    )

    with db_engine.connect() as conn:
        remaining = {r.symbol for r in conn.execute(sa.select(db.symbol_master.c.symbol))}
    assert (count, removed) == (1, 1)
    assert remaining == {"005930", "247540"}  # 다른 시장(KOSDAQ)은 건드리지 않는다


def test_load_stock_symbols_returns_only_common_stocks(db_engine):
    db.migrate(db_engine)
    stock = master.parse_master(
        build_line("KOSPI", "005930", "삼성전자", **SAMSUNG_FIELDS), "KOSPI"
    )[0]
    etf_fields = {**SAMSUNG_FIELDS, "그룹코드": "EF"}
    etf = master.parse_master(build_line("KOSPI", "069500", "KODEX200", **etf_fields), "KOSPI")[0]
    master.upsert_master(db_engine, [stock, etf], updated_at=datetime(2026, 8, 3))

    assert master.load_stock_symbols(db_engine) == ["005930"]

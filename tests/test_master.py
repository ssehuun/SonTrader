"""종목 마스터 파싱/적재 테스트 — 합성 .mst 라인과 인메모리 zip 사용."""

import io
import zipfile
from datetime import date, datetime, timedelta

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
    상장일자="19750611",
    상장주수="5969782550",
    지수업종대분류="0001",
    매매수량단위="00001",
)

# 오늘. 상장일자 필터가 날짜에 의존하므로 테스트는 시각을 주입한다.
TODAY = date(2026, 8, 16)


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
    assert row.listing_date == date(1975, 6, 11)
    assert row.shares_outstanding == 5969782550
    assert row.sector_large == "0001"
    assert row.trading_unit == 1


def test_parse_tolerates_missing_listing_date():
    """상장일자가 비어 있어도 파싱은 성공하고 None이 된다 — 판정은 필터 몫."""
    fields = {**SAMSUNG_FIELDS, "상장일자": "        "}
    (row,) = master.parse_master(build_line("KOSPI", "005930", "삼성전자", **fields), "KOSPI")

    assert row.listing_date is None


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


def _row(symbol, name, **overrides):
    fields = {**SAMSUNG_FIELDS, **overrides}
    return master.parse_master(build_line("KOSPI", symbol, name, **fields), "KOSPI")[0]


def test_load_collectable_symbols_applies_structural_filter(db_engine):
    """수집 대상은 구조적 속성으로만 걸러진다 — 시변 상태는 보지 않는다.

    관리종목은 오늘 값일 뿐 과거엔 정상이었을 수 있으므로, 수집 단계에서
    빼면 백테스트에 생존 편향이 들어간다. 그래서 여기서는 통과해야 한다.
    """
    db.migrate(db_engine)
    rows = [
        _row("005930", "삼성전자"),
        _row("069500", "KODEX200", 그룹코드="EF"),
        _row("005935", "삼성전자우", 우선주="1"),
        _row("123456", "교보18호스팩", SPAC="Y"),
        _row("234567", "신규상장사", 상장일자="20260301"),  # 상장 400일 미만
        _row("345678", "상장일불명", 상장일자="        "),
        _row("456789", "관리종목이지만수집", 관리종목="Y"),
    ]
    master.upsert_master(db_engine, rows, updated_at=datetime(2026, 8, 3))

    assert master.load_collectable_symbols(db_engine, today=TODAY) == ["005930", "456789"]


def test_load_collectable_symbols_boundary_of_listing_age(db_engine):
    """상장 400일 경계: 정확히 400일이면 통과, 399일이면 제외."""
    db.migrate(db_engine)
    master.upsert_master(
        db_engine,
        [
            _row("111111", "딱400일", 상장일자=(TODAY - timedelta(days=400)).strftime("%Y%m%d")),
            _row("222222", "399일", 상장일자=(TODAY - timedelta(days=399)).strftime("%Y%m%d")),
        ],
        updated_at=datetime(2026, 8, 3),
    )

    assert master.load_collectable_symbols(db_engine, today=TODAY) == ["111111"]


def test_empty_universe_hint_distinguishes_empty_table_from_filtered_out(db_engine):
    """수집 대상 0종목의 두 원인을 구분해 안내한다.

    listing_date 컬럼을 추가한 마이그레이션 직후 collect-master를 다시 돌리기
    전에는 전 행이 NULL이라 fail-closed로 전량 제외된다. 이때 "테이블이 비었다"고
    안내하면 원인을 못 찾는다 — 실제로 이 순서로 실행하면 발생한다.
    """
    from sontrader.cli import _empty_universe_hint

    db.migrate(db_engine)
    assert "is empty" in _empty_universe_hint(db_engine)

    # 마스터는 있지만 상장일자가 없는 상태 (마이그레이션 직후)
    master.upsert_master(
        db_engine,
        [_row("005930", "삼성전자", 상장일자="        ")],
        updated_at=datetime(2026, 8, 3),
    )

    hint = _empty_universe_hint(db_engine)
    assert "1종목이 있지만" in hint
    assert "collect-master" in hint
    assert master.load_collectable_symbols(db_engine, today=TODAY) == []

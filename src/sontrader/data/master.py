"""KOSPI/KOSDAQ 종목 마스터 수집 (구현 계획 3단계).

KIS가 배포하는 .mst 마스터 파일을 내려받아 ``symbol_master``에 upsert한다.
파일은 「단축코드(9) 표준코드(12) 한글명(가변)」 머리부와 고정폭 꼬리부로
구성되며, 꼬리부 폭 명세는 kis_trading의 master_manager에서 검증된 것을
그대로 옮겼다 (pandas 없이 순수 슬라이싱).

플래그 값('Y'/'N', 코드 문자)은 해석 없이 원본 그대로 저장한다 — 유니버스
필터(3b, core)가 소비하는 시점에 해석한다. 원본 파일 인코딩은 cp949.
"""

from __future__ import annotations

import dataclasses
import io
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime

import httpx
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.core.filters import StructuralInfo, is_collectable
from sontrader.data import db

MASTER_URLS = {
    "KOSPI": "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip",
    "KOSDAQ": "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip",
}

# 꼬리부 고정폭 명세 — kis_trading managers/master_manager.py에서 그대로 가져옴.
# fmt: off
_KOSPI_WIDTHS = [
    2, 1, 4, 4, 4, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 9, 5, 5, 1, 1, 1, 2, 1, 1,
    1, 2, 2, 2, 3, 1, 3, 12, 12, 8, 15, 21, 2, 7, 1, 1, 1, 1, 1, 9,
    9, 9, 5, 9, 8, 9, 3, 1, 1, 1,
]
_KOSPI_NAMES = [
    "그룹코드", "시가총액규모", "지수업종대분류", "지수업종중분류", "지수업종소분류",
    "제조업", "저유동성", "지배구조지수종목", "KOSPI200섹터업종", "KOSPI100",
    "KOSPI50", "KRX", "ETP", "ELW발행", "KRX100",
    "KRX자동차", "KRX반도체", "KRX바이오", "KRX은행", "SPAC",
    "KRX에너지화학", "KRX철강", "단기과열", "KRX미디어통신", "KRX건설",
    "Non1", "KRX증권", "KRX선박", "KRX섹터_보험", "KRX섹터_운송",
    "SRI", "기준가", "매매수량단위", "시간외수량단위", "거래정지",
    "정리매매", "관리종목", "시장경고", "경고예고", "불성실공시",
    "우회상장", "락구분", "액면변경", "증자구분", "증거금비율",
    "신용가능", "신용기간", "전일거래량", "액면가", "상장일자",
    "상장주수", "자본금", "결산월", "공모가", "우선주",
    "공매도과열", "이상급등", "KRX300", "KOSPI", "매출액",
    "영업이익", "경상이익", "당기순이익", "ROE", "기준년월",
    "시가총액", "그룹사코드", "회사신용한도초과", "담보대출가능", "대주가능",
]
_KOSDAQ_WIDTHS = [
    2, 1, 4, 4, 4, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 9, 5, 5, 1, 1, 1, 2, 1, 1, 1, 2, 2, 2, 3,
    1, 3, 12, 12, 8, 15, 21, 2, 7, 1, 1, 1, 1, 9, 9, 9, 5, 9, 8, 9,
    3, 1, 1, 1,
]
_KOSDAQ_NAMES = [
    "그룹코드", "시가총액규모", "지수업종대분류", "지수업종중분류", "지수업종소분류",
    "벤처기업여부", "저유동성", "KRX", "ETP", "KRX100",
    "KRX자동차", "KRX반도체", "KRX바이오", "KRX은행", "SPAC",
    "KRX에너지화학", "KRX철강", "단기과열", "KRX미디어통신", "KRX건설",
    "투자주의환기", "KRX증권", "KRX선박", "KRX섹터_보험", "KRX섹터_운송",
    "KOSDAQ150", "기준가", "매매수량단위", "시간외수량단위", "거래정지",
    "정리매매", "관리종목", "시장경고", "경고예고", "불성실공시",
    "우회상장", "락구분", "액면변경", "증자구분", "증거금비율",
    "신용가능", "신용기간", "전일거래량", "액면가", "상장일자",
    "상장주수", "자본금", "결산월", "공모가", "우선주",
    "공매도과열", "이상급등", "KRX300", "매출액", "영업이익",
    "경상이익", "당기순이익", "ROE", "기준년월", "시가총액",
    "그룹사코드", "회사신용한도초과", "담보대출가능", "대주가능",
]
# fmt: on

# symbol_master 컬럼 → 꼬리부 필드명 (두 시장의 명세가 같은 이름을 쓰도록
# _KOSDAQ_NAMES를 정리했으므로 공용 매핑 하나로 충분하다).
_FIELD_MAP = {
    "group_code": "그룹코드",
    "cap_scale_code": "시가총액규모",
    "low_liquidity_yn": "저유동성",
    "spac_yn": "SPAC",
    "pref_share_code": "우선주",
    "base_price": "기준가",
    "suspended_yn": "거래정지",
    "liquidation_yn": "정리매매",
    "managed_yn": "관리종목",
    "market_warning_code": "시장경고",
    "unfaithful_yn": "불성실공시",
    "prev_volume": "전일거래량",
    "op_profit": "영업이익",
    "market_cap": "시가총액",
    "listing_date": "상장일자",
    "shares_outstanding": "상장주수",
    "sector_large": "지수업종대분류",
    "sector_mid": "지수업종중분류",
    "sector_small": "지수업종소분류",
    "trading_unit": "매매수량단위",
}
_INT_COLUMNS = {
    "base_price",
    "prev_volume",
    "op_profit",
    "market_cap",
    "shares_outstanding",
    "trading_unit",
}
_DATE_COLUMNS = {"listing_date"}


@dataclass(frozen=True)
class MasterRow:
    symbol: str
    name: str
    market: str
    group_code: str
    cap_scale_code: str
    low_liquidity_yn: str
    spac_yn: str
    pref_share_code: str
    base_price: int | None
    suspended_yn: str
    liquidation_yn: str
    managed_yn: str
    market_warning_code: str
    unfaithful_yn: str
    prev_volume: int | None
    op_profit: int | None
    market_cap: int | None
    listing_date: date | None
    shares_outstanding: int | None
    sector_large: str
    sector_mid: str
    sector_small: str
    trading_unit: int | None


def _field_slices(widths: list[int], names: list[str]) -> dict[str, slice]:
    slices, offset = {}, 0
    for width, name in zip(widths, names, strict=True):
        slices[name] = slice(offset, offset + width)
        offset += width
    return slices


_SPECS = {
    "KOSPI": (_field_slices(_KOSPI_WIDTHS, _KOSPI_NAMES), sum(_KOSPI_WIDTHS)),
    "KOSDAQ": (_field_slices(_KOSDAQ_WIDTHS, _KOSDAQ_NAMES), sum(_KOSDAQ_WIDTHS)),
}


def parse_master(text: str, market: str) -> list[MasterRow]:
    """마스터 파일 본문(cp949 디코딩 후)을 파싱한다. 순수 함수 — 테스트 대상."""
    slices, tail_width = _SPECS[market]
    rows: list[MasterRow] = []
    for line in text.splitlines():
        if len(line) <= tail_width + 21:  # 머리부(21) + 꼬리부보다 짧으면 불량 행
            continue
        head, tail = line[:-tail_width], line[-tail_width:]
        symbol = head[0:9].rstrip()
        name = head[21:].strip()
        if not symbol or not name:
            continue

        values: dict[str, object] = {}
        for column, field_name in _FIELD_MAP.items():
            raw = tail[slices[field_name]].strip()
            if column in _INT_COLUMNS:
                values[column] = _to_int(raw)
            elif column in _DATE_COLUMNS:
                values[column] = _to_date(raw)
            else:
                values[column] = raw
        rows.append(MasterRow(symbol=symbol, name=name, market=market, **values))
    return rows


def _to_int(raw: str) -> int | None:
    try:
        return int(raw)
    except ValueError:
        return None


def _to_date(raw: str) -> date | None:
    """YYYYMMDD → date. 미상장·불량 값은 None (필터가 fail-closed로 처리한다)."""
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        return None


def fetch_master(market: str, transport: httpx.BaseTransport | None = None) -> list[MasterRow]:
    """마스터 zip 다운로드 → .mst 추출(cp949) → 파싱."""
    with httpx.Client(timeout=60.0, follow_redirects=True, transport=transport) as http:
        response = http.get(MASTER_URLS[market])
        response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        mst_name = next(n for n in archive.namelist() if n.lower().endswith(".mst"))
        text = archive.read(mst_name).decode("cp949")
    return parse_master(text, market)


def upsert_master(engine: Engine, rows: list[MasterRow], updated_at: datetime) -> tuple[int, int]:
    """symbol PK 기준 upsert + 상장폐지 정리. 반환값은 (갱신 수, 제거 수).

    이번 파일에 없는 같은 시장의 기존 종목은 상장폐지로 보고 제거한다 —
    남겨두면 마지막 플래그가 얼어붙은 채 수집·유니버스가 계속 소비한다.
    """
    if not rows:
        return 0, 0
    market = rows[0].market
    payload = [{**asdict(r), "updated_at": updated_at} for r in rows]
    current = {r.symbol for r in rows}
    with engine.begin() as conn:
        db.upsert_rows(conn, db.symbol_master, payload, key_cols=("symbol",))
        existing = {
            row.symbol
            for row in conn.execute(
                sa.select(db.symbol_master.c.symbol).where(db.symbol_master.c.market == market)
            )
        }
        stale = sorted(existing - current)
        for start in range(0, len(stale), 500):
            conn.execute(
                sa.delete(db.symbol_master).where(
                    db.symbol_master.c.market == market,
                    db.symbol_master.c.symbol.in_(stale[start : start + 500]),
                )
            )
    return len(rows), len(stale)


def load_stock_symbols(engine: Engine, *, today: date) -> list[str]:
    """일봉 수집 대상. `core.filters.is_collectable()`이 판정한다 —
    구조적 속성만 보므로 백테스트에 편향이 들어가지 않는다(그 함수의
    모듈 독스트링 참고). 시변 상태(관리종목·거래정지 등)로 거르는 것은
    유니버스 산출 시점의 `is_tradeable()` 몫이다.

    컬럼 목록은 StructuralInfo 필드에서 파생 — 둘이 어긋날 수 없다.
    """
    columns = [db.symbol_master.c[field.name] for field in dataclasses.fields(StructuralInfo)]
    with engine.connect() as conn:
        rows = conn.execute(sa.select(*columns).order_by(db.symbol_master.c.symbol))
        return [
            row.symbol
            for row in rows
            if is_collectable(StructuralInfo(**row._mapping), today=today)
        ]

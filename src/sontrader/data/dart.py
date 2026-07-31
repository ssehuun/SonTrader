"""DART 공시 수집기 (구현 계획 2단계).

Polls the OpenDART ``list.json`` endpoint and appends disclosures to the
``events`` table:

- Dual timestamps: ``published_at`` (DART 접수일) and ``ingested_at`` (수집
  시각) are recorded separately. Backtests replay by ``ingested_at`` — the
  list API only gives the filing *date*, which is fine because replay never
  uses ``published_at`` (설계 2.1절).
- Idempotent: ``event_id`` (= 접수번호 rcept_no) is the PK and inserts use
  ON CONFLICT DO NOTHING, so re-running a collection is safe.
- Normalization: ``norm_key`` = corp_code + 분기 + 정정 접두어를 벗긴 제목.
  It merges re-filings ([기재정정] 등) of the same disclosure and scopes
  recurring same-titled disclosures (공급계약 등) to a quarter. It does NOT
  merge 잠정실적 with the later 사업보고서 — their titles differ; that pairing
  is the gate's job via a (corp, event_type) cooldown (설계 4.2절).
- ``event_type``/``norm_key`` are *derived* from the title: the append-only
  rule protects the source fields (rcept_no, title, timestamps, raw_json);
  derived columns may be recomputed when the classifier improves.

Why OpenDART instead of the todayRSS.xml feed the dart_noti repo uses: RSS
carries no stock code or corp_code, so events could never be matched against
the watchlist. The trade-off (filing time-of-day is lost) is acceptable
because ingested_at is the replay clock. Corollary: rows backfilled with
``--date`` get ingested_at = collection time, which makes them unusable for
ingested_at replay — live collection has to run continuously (계획서가 2단계를
앞당긴 이유), and the polling interval bounds the modeled signal latency.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.data import db

LIST_URL = "https://opendart.fss.or.kr/api/list.json"
_PAGE_COUNT = 100  # OpenDART 최대 페이지 크기

# corp_cls: Y=유가, K=코스닥, N=코넥스, E=기타. 매매 대상 시장만 수집한다.
WATCHED_MARKETS = frozenset({"Y", "K"})


class DartError(RuntimeError):
    """OpenDART answered with a non-success status code (or unusable body)."""

    def __init__(self, status: str, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status


# 재시도가 무의미한 상태 코드: 미등록/사용 불가 키, 접근 권한 없음.
FATAL_STATUSES = frozenset({"010", "011", "012"})


@dataclass(frozen=True)
class Disclosure:
    rcept_no: str  # 접수번호 — 전역 고유, event_id로 사용
    corp_code: str  # DART 고유번호 (8자리)
    corp_name: str
    stock_code: str | None  # 6자리 종목코드, 비상장이면 None
    corp_cls: str
    report_nm: str
    rcept_dt: date


class DartClient:
    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None):
        self._api_key = api_key
        self._http = httpx.Client(timeout=30.0, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> DartClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def list_disclosures(self, day: date) -> list[Disclosure]:
        """하루치 전종목 공시 (유가/코스닥), 페이지네이션 포함."""
        disclosures: list[Disclosure] = []
        page = 1
        while True:
            data = self._fetch_page(day, page)
            status = data.get("status")
            if status == "013":  # 조회된 데이터가 없습니다
                return disclosures
            if status != "000":
                raise DartError(str(status), str(data.get("message", "")))
            for item in data.get("list", []):
                parsed = _parse_item(item)
                if parsed is not None and parsed.corp_cls in WATCHED_MARKETS:
                    disclosures.append(parsed)
            if page >= int(data.get("total_page") or 1):
                return disclosures
            page += 1

    def _fetch_page(self, day: date, page: int) -> dict[str, Any]:
        response = self._http.get(
            LIST_URL,
            params={
                "crtfc_key": self._api_key,
                "bgn_de": day.strftime("%Y%m%d"),
                "end_de": day.strftime("%Y%m%d"),
                "page_no": str(page),
                "page_count": str(_PAGE_COUNT),
            },
        )
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as exc:
            # HTTP 200이지만 JSON이 아닌 본문 (점검 페이지 등) — 폴링 루프가
            # 다음 주기에 재시도할 수 있도록 DartError로 변환한다.
            raise DartError("BAD_BODY", "response is not JSON") from exc


def _parse_item(item: dict[str, Any]) -> Disclosure | None:
    def field(key: str) -> str:
        # 키 부재뿐 아니라 JSON null도 방어한다 (item.get(k, "")는 null을 못 거른다).
        return (item.get(key) or "").strip()

    rcept_no = field("rcept_no")
    if not rcept_no:
        return None
    try:
        rcept_dt = datetime.strptime(field("rcept_dt"), "%Y%m%d").date()
    except ValueError:
        try:
            # 접수번호 앞 8자리가 접수일자다 (YYYYMMDDNNNNNN).
            rcept_dt = datetime.strptime(rcept_no[:8], "%Y%m%d").date()
        except ValueError:
            return None  # 날짜를 복원할 수 없는 행은 버리고 수집은 계속한다
    return Disclosure(
        rcept_no=rcept_no,
        corp_code=field("corp_code"),
        corp_name=field("corp_name"),
        stock_code=field("stock_code") or None,
        corp_cls=field("corp_cls"),
        report_nm=field("report_nm"),
        rcept_dt=rcept_dt,
    )


# --- 정규화 ---------------------------------------------------------------

# 재공시 접두어만 벗긴다: "[기재정정]", "[첨부정정]", "[첨부추가]".
# "[발행조건확정]"처럼 정정이 아닌 대괄호 태그는 별개 이벤트이므로 보존한다.
_REFILING_PREFIX = re.compile(r"^(?:\[[^\]]*정정[^\]]*\]\s*|\[첨부추가\]\s*)+")
_WHITESPACE = re.compile(r"\s+")

# report_nm 부분 문자열 → event_type. 위에서부터 첫 매치가 이긴다.
_EVENT_TYPES: list[tuple[str, str]] = [
    ("잠정실적", "earnings"),
    ("(잠정)실적", "earnings"),
    ("영업실적", "earnings"),
    ("사업보고서", "earnings"),
    ("반기보고서", "earnings"),
    ("분기보고서", "earnings"),
    ("유상증자", "capital_change"),
    ("무상증자", "capital_change"),
    ("감자", "capital_change"),
    ("전환사채", "capital_change"),
    ("신주인수권부사채", "capital_change"),
    # 주식분할/액면분할은 기업분할(mna)이 아니므로 "분할"보다 먼저 매칭해야 한다.
    ("주식분할", "capital_change"),
    ("액면분할", "capital_change"),
    ("합병", "mna"),
    ("분할", "mna"),
    ("영업양수", "mna"),
    ("영업양도", "mna"),
    # 처분(매도 신호)과 취득(매수 신호)은 방향이 반대라 같은 유형이면 안 된다.
    ("자기주식처분", "share_disposal"),
    ("자기주식", "buyback"),
    ("공급계약", "supply_contract"),
]


def classify(report_nm: str) -> str:
    normalized = normalize_title(report_nm)
    for needle, event_type in _EVENT_TYPES:
        if needle in normalized:
            return event_type
    return "other"


def normalize_title(report_nm: str) -> str:
    return _WHITESPACE.sub("", _REFILING_PREFIX.sub("", report_nm))


def norm_key(disclosure: Disclosure) -> str:
    """corp_code + 분기 + 정규화 제목. 컬럼 폭(200)에 맞게 자른다.

    분기를 넣는 이유: 같은 회사가 몇 달 간격으로 내는 동일 제목 공시(공급계약
    등)는 별개 이벤트인데, 제목만으로 묶으면 영구히 과병합된다. 정정 재공시는
    통상 며칠 안에 나오므로 분기 스코프 안에서 원본과 합쳐진다.
    """
    quarter = (disclosure.rcept_dt.month - 1) // 3 + 1
    key = (
        f"{disclosure.corp_code}:{disclosure.rcept_dt.year}Q{quarter}:"
        f"{normalize_title(disclosure.report_nm)}"
    )
    return key[:200]


# --- 적재 -----------------------------------------------------------------


# SQLite 구버전의 바인드 변수 한도(999) 안에 들어가는 배치 크기.
_INSERT_CHUNK = 100


def ingest(engine: Engine, disclosures: list[Disclosure], ingested_at: datetime) -> int:
    """Append new disclosures to events; returns how many were new.

    Rows whose event_id already exists are skipped (ON CONFLICT DO NOTHING),
    which is what makes re-collection idempotent. ``ingested_at`` is injected
    by the caller so tests and replays control the clock. The new-row count
    comes from before/after COUNT within the same transaction — multi-row
    insert rowcounts are not reliable across dialects.
    """
    # 페이지네이션 도중 목록이 밀리면 같은 접수번호가 두 번 올 수 있다.
    unique = {d.rcept_no: d for d in disclosures}
    if not unique:
        return 0
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert

    rows = [_event_row(d, ingested_at) for d in unique.values()]
    count_stmt = sa.select(sa.func.count()).select_from(db.events)
    with engine.begin() as conn:
        before = conn.execute(count_stmt).scalar_one()
        for start in range(0, len(rows), _INSERT_CHUNK):
            stmt = (
                insert(db.events)
                .values(rows[start : start + _INSERT_CHUNK])
                .on_conflict_do_nothing(index_elements=["event_id"])
            )
            conn.execute(stmt)
        after = conn.execute(count_stmt).scalar_one()
    return after - before


def _event_row(d: Disclosure, ingested_at: datetime) -> dict[str, Any]:
    return dict(
        event_id=d.rcept_no,
        symbol=d.stock_code,
        corp_code=d.corp_code,
        event_type=classify(d.report_nm),
        norm_key=norm_key(d),
        title=d.report_nm,
        published_at=datetime.combine(d.rcept_dt, time.min),
        ingested_at=ingested_at,
        raw_json={**asdict(d), "rcept_dt": d.rcept_dt.isoformat()},
    )

"""국내 거래일 캘린더 (구현 계획 번호 없음 — 01문서 §8 미확정 파라미터
"거래일 캘린더 소스"). KIS 국내휴장일조회(CTCA0903R)를 원본으로 쓴다 —
이미 있는 인증·클라이언트 인프라를 재사용하고, "그날 KIS가 실제로
주문을 받아줄지"를 가장 직접적으로 알려주는 소스이기 때문이다.

## 왜 매 사이클 호출하지 않는가

KIS 문서가 "원장서비스와 연관돼 있어 가급적 1일 1회 호출"을 명시적으로
요청한다. 한 번 호출하면 기준일부터 몇 주치 영업일 정보가 한꺼번에
돌아오므로, `refresh_if_needed()`는 오늘 날짜가 이미 캐시에 있으면
아무것도 하지 않는다 — 그래서 실제 호출 빈도는 몇 주에 한 번꼴이 되고
"1일 1회" 상한을 넘길 일이 없다.

## 모의투자에서는 쓸 수 없다

TR_ID CTCA0903R는 실전 전용이다(`KisClient.get_market_holidays()`가
호출 전에 막는다). 모의투자 계좌에서는 이 모듈을 아예 쓰지 않는다 —
호출자(`apps/live.py`)가 `settings.paper`를 보고 건너뛴다.
"""

from __future__ import annotations

from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.client import KisClient
from sontrader.data import db


def refresh_if_needed(engine: Engine, client: KisClient, *, today: date) -> None:
    """오늘 날짜가 캐시에 이미 있으면 아무것도 하지 않는다."""
    if is_open(engine, today) is not None:
        return
    rows = client.get_market_holidays(today)
    _store(engine, rows)


def is_open(engine: Engine, day: date) -> bool | None:
    """개장일이면 True, 휴장일이면 False, 캐시에 아직 없으면 None."""
    columns = db.market_calendar.c
    with engine.connect() as conn:
        row = conn.execute(sa.select(columns.open_yn).where(columns.date == day)).first()
    return None if row is None else row.open_yn == "Y"


def _store(engine: Engine, rows: list[dict]) -> None:
    if not rows:
        return
    values = [
        {
            "date": datetime.strptime(row["bass_dt"], "%Y%m%d").date(),
            "open_yn": row["opnd_yn"],
            "business_day_yn": row["bzdy_yn"],
            "trading_day_yn": row["tr_day_yn"],
            "settlement_day_yn": row["sttl_day_yn"],
        }
        for row in rows
    ]
    with engine.begin() as conn:
        db.upsert_rows(conn, db.market_calendar, values, key_cols=("date",))

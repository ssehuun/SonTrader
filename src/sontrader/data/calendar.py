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

from datetime import date, datetime, time

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.client import KisClient
from sontrader.data import db

# 장 운영시간 (KST). 순수 상수 + 순수 함수라 시각을 인자로 받는다.
#
# 시작을 09:00이 아니라 08:30으로 잡는 이유: 장 전 동시호가가 08:30에 시작하고,
# 이때 낸 시장가 주문은 시초가에 체결된다. 설계 1.3절이 "진입은 다음 개장
# 시가"라고 정한 것과 정확히 맞는 타이밍이라, 09:00까지 기다리면 오히려
# 의도한 체결 시점을 놓친다 (L6 검증에서 08:45 주문이 시초가 체결됨을 확인).
#
# 종료는 정규장 마감 15:30. 시간외 거래는 이 시스템이 다루지 않는다.
TRADING_START = time(8, 30)
TRADING_END = time(15, 30)

# 오늘 일봉이 확정되는 시각. 마감 + 체결 정리 여유. 이 전에 받은 오늘 봉은
# 임시 종가라 저장하면 안 된다 (data/prices.py의 include_today 참고).
BAR_FINAL_AFTER = time(15, 40)


def is_market_hours(now: datetime) -> bool:
    """지금이 주문을 낼 수 있는 시간대인가. **휴장일 여부는 보지 않는다** —
    그건 `is_open()`의 몫이고, 둘은 각각 판정해 함께 쓴다."""
    return TRADING_START <= now.time() < TRADING_END


def today_bar_is_final(now: datetime) -> bool:
    """오늘 일봉이 확정됐는가 (수집기가 저장해도 되는가)."""
    return now.time() >= BAR_FINAL_AFTER


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

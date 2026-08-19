"""사이클 감사 기록 테스트.

"2026-03-15에 왜 안 샀나"에 답할 수 있어야 한다는 것이 이 표의 존재 이유다.
텍스트 로그로는 슬롯이 찼는지, 쿨다운이었는지, 봇이 죽어 있었는지를
구분할 수 없다.
"""

from datetime import datetime, timedelta

import sqlalchemy as sa

from sontrader.core.gate import Rejection, RejectReason
from sontrader.data import cycle_log, db

TS = datetime(2026, 8, 20, 9, 30)


def rows(engine):
    with engine.connect() as conn:
        return conn.execute(sa.select(db.cycle_log).order_by(db.cycle_log.c.ts)).mappings().all()


def test_records_a_cycle(db_engine):
    db.migrate(db_engine)

    cycle_log.record(
        db_engine,
        ts=TS,
        watchlist_n=38,
        positions_n=2,
        cash=1_000,
        equity=9_500_000,
        pending_n=3,
        orders_n=1,
    )

    (row,) = rows(db_engine)
    assert row["ts"] == TS
    assert (row["watchlist_n"], row["positions_n"], row["orders_n"]) == (38, 2, 1)
    assert row["equity"] == 9_500_000
    assert row["halted"] is False
    assert row["rejections"] == []


def test_rejection_reasons_survive_for_later_analysis(db_engine):
    """'왜 안 샀나'가 사유별로 남아야 한다 — 이 표의 핵심 목적."""
    db.migrate(db_engine)

    cycle_log.record(
        db_engine,
        ts=TS,
        watchlist_n=38,
        positions_n=5,
        cash=0,
        equity=9_000_000,
        rejections=(
            Rejection("005930", RejectReason.SLOT_FULL),
            Rejection("000660", RejectReason.COOLDOWN, "E-1"),
        ),
    )

    (row,) = rows(db_engine)
    assert row["rejections"] == [
        {"symbol": "005930", "reason": "slot_full", "event_id": None},
        {"symbol": "000660", "reason": "cooldown", "event_id": "E-1"},
    ]


def test_halt_is_recorded_not_just_missing(db_engine):
    """중단은 행으로 남아야 한다 — 구멍만 있으면 '죽었다'와 '멈췄다'가 같아 보인다."""
    db.migrate(db_engine)

    cycle_log.record(db_engine, ts=TS, watchlist_n=0, positions_n=1, cash=0, equity=0, halted=True)

    (row,) = rows(db_engine)
    assert row["halted"] is True


def test_same_timestamp_is_upserted(db_engine):
    """재실행·재시작이 겹쳐도 행이 두 번 쌓이지 않는다."""
    db.migrate(db_engine)

    cycle_log.record(db_engine, ts=TS, watchlist_n=1, positions_n=0, cash=0, equity=1)
    cycle_log.record(db_engine, ts=TS, watchlist_n=2, positions_n=0, cash=0, equity=2)

    stored = rows(db_engine)
    assert len(stored) == 1
    assert stored[0]["watchlist_n"] == 2


def test_gaps_reveal_downtime(db_engine):
    """장중 60초 주기이므로 행의 구멍이 곧 다운타임이다."""
    db.migrate(db_engine)
    for minutes in (0, 1, 5):  # 2~4분이 비어 있다
        cycle_log.record(
            db_engine,
            ts=TS + timedelta(minutes=minutes),
            watchlist_n=1,
            positions_n=0,
            cash=0,
            equity=1,
        )

    stamps = [r["ts"] for r in rows(db_engine)]
    gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:], strict=False)]
    assert max(gaps) == 240  # 4분 공백이 드러난다

"""Schema/migration tests — run on SQLite in-memory (`db_engine` fixture),
no DB server needed. FK enforcement is on to mirror PostgreSQL."""

from datetime import datetime

import pytest
import sqlalchemy as sa

from sontrader.data import db

NEW_TABLES = {
    "events",
    "llm_judgments",
    "orders",
    "fills",
    "positions",
    "kill_switch",
    "symbol_master",
    "stock_candles_1d",
    "stock_candles_1m",
    "market_calendar",
    "watchlist_snapshots",
    "daytrade_watchlist_snapshots",
    "index_candles_1d",
    "cycle_log",
}


def test_migrate_creates_all_new_tables(db_engine):
    actions = db.migrate(db_engine)

    assert NEW_TABLES <= set(sa.inspect(db_engine).get_table_names())
    assert len(actions) == len(NEW_TABLES)


def test_migrate_is_idempotent(db_engine):
    db.migrate(db_engine)

    assert db.migrate(db_engine) == []


def test_legacy_candles_gain_adjustment_columns(db_engine):
    # kis_trading이 만들어 둔 기존 일봉 테이블 (수정주가 컬럼 없음)을 흉내낸다.
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE stock_candles_1d ("
                "symbol VARCHAR(20), date DATE, open INTEGER, high INTEGER, "
                "low INTEGER, close INTEGER, volume BIGINT, trade_value BIGINT, "
                "prdy_vrss INTEGER, prdy_ctrt FLOAT, "
                "PRIMARY KEY (symbol, date))"
            )
        )

    actions = db.migrate(db_engine)

    cols = {c["name"] for c in sa.inspect(db_engine).get_columns("stock_candles_1d")}
    assert {"flng_cls_code", "prtt_rate", "mod_yn", "adj_factor"} <= cols
    assert sum("stock_candles_1d" in a for a in actions) == 4
    assert db.migrate(db_engine) == []  # 재실행 시 컬럼 중복 추가 없음


def test_migrate_syncs_columns_and_indexes_of_existing_tables(db_engine):
    # norm_key 이전 버전 스키마를 흉내: 컬럼도 인덱스도 없는 events 테이블.
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE events ("
                "event_id VARCHAR(32) PRIMARY KEY, "
                "symbol VARCHAR(20), corp_code VARCHAR(20), event_type VARCHAR(50), "
                "title TEXT, published_at TIMESTAMP NOT NULL, "
                "ingested_at TIMESTAMP NOT NULL, raw_json JSON)"
            )
        )

    actions = db.migrate(db_engine)

    assert "added column events.norm_key" in actions
    assert "created index ix_events_norm_key" in actions
    index_names = {ix["name"] for ix in sa.inspect(db_engine).get_indexes("events")}
    assert {"ix_events_norm_key", "ix_events_symbol", "ix_events_ingested_at"} <= index_names
    assert db.migrate(db_engine) == []


def test_migrate_adds_missing_columns_to_owned_tables(db_engine):
    # 과거 버전 스키마(컬럼 하나 부족)를 흉내 내면, migrate가 그 컬럼을 채워야 한다.
    # 테이블이 이미 존재한다는 이유로 조용히 건너뛰면 스키마 드리프트가 된다.
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE positions ("
                "symbol VARCHAR(20) PRIMARY KEY, "
                "qty INTEGER NOT NULL, "
                "avg_price NUMERIC(16, 4) NOT NULL)"
            )
        )

    actions = db.migrate(db_engine)

    assert "added column positions.entered_at" in actions
    cols = {c["name"] for c in sa.inspect(db_engine).get_columns("positions")}
    assert "entered_at" in cols
    assert db.migrate(db_engine) == []


def test_orders_idempotency_key_is_unique(db_engine):
    db.migrate(db_engine)

    def order_row(order_id):
        return dict(
            order_id=order_id,
            idempotency_key="entry:20260731000001:005930",
            symbol="005930",
            side="buy",
            qty=10,
            order_type="market",
            urgency="NEXT_OPEN",
            status="submitted",
            created_at=datetime(2026, 7, 31, 9, 0, 0),
        )

    with db_engine.begin() as conn:
        conn.execute(db.orders.insert().values(**order_row("order-1")))
    with pytest.raises(sa.exc.IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(db.orders.insert().values(**order_row("order-2")))


def test_llm_judgment_pk_allows_reruns_only_with_new_version(db_engine):
    db.migrate(db_engine)

    def judgment_row(prompt_version):
        return dict(
            event_id="20260731000001",
            prompt_version=prompt_version,
            model="claude-fable-5",
            verdict=True,
            confidence=0.8,
            exit_rule_json={"손절률": -0.05},
            rationale="test",
            created_at=datetime(2026, 7, 31, 16, 2, 0),
        )

    with db_engine.begin() as conn:
        conn.execute(
            db.events.insert().values(
                event_id="20260731000001",
                symbol="005930",
                corp_code="00126380",
                event_type="earnings",
                title="연결재무제표 기준 영업(잠정)실적",
                published_at=datetime(2026, 7, 31, 16, 0, 0),
                ingested_at=datetime(2026, 7, 31, 16, 1, 12),
                raw_json={"rcept_no": "20260731000001"},
            )
        )
        conn.execute(db.llm_judgments.insert().values(**judgment_row("v1")))
        conn.execute(db.llm_judgments.insert().values(**judgment_row("v2")))  # 버전이 다르면 허용
    with pytest.raises(sa.exc.IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(db.llm_judgments.insert().values(**judgment_row("v1")))


def test_judgment_for_unknown_event_is_rejected(db_engine):
    # FK가 실제로 강제되는지 확인 (PostgreSQL과 동일하게).
    db.migrate(db_engine)

    with pytest.raises(sa.exc.IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                db.llm_judgments.insert().values(
                    event_id="no-such-event",
                    prompt_version="v1",
                    model="claude-fable-5",
                    verdict=False,
                    confidence=0.1,
                    created_at=datetime(2026, 7, 31, 16, 2, 0),
                )
            )


def test_positions_has_no_high_water_column():
    # 설계 6.5절: high_water는 파생 데이터라 저장하지 않는다.
    assert "high_water" not in {c.name for c in db.positions.columns}


# --- 스키마 드리프트 점검 --------------------------------------------------------


def test_pending_migrations_is_empty_after_migrate(db_engine):
    assert db.pending_migrations(db_engine)  # 빈 DB에는 할 일이 있다
    db.migrate(db_engine)

    assert db.pending_migrations(db_engine) == []


def test_pending_migrations_does_not_touch_the_database(db_engine):
    """점검이 스키마를 고쳐 버리면 '적용 전에 무엇을 할지 남긴다'가 불가능해진다."""
    before = sorted(sa.inspect(db_engine).get_table_names())

    db.pending_migrations(db_engine)

    assert sorted(sa.inspect(db_engine).get_table_names()) == before


def test_pending_migrations_reports_a_missing_column(db_engine):
    """실제로 두 번 겪은 사고의 회귀 테스트. 테이블은 있고 나중에 추가된 컬럼만
    없는 DB — 이 상태로 기동하면 장중에 psycopg2 UndefinedColumn으로 터진다
    (`orders.exit_rule_json`, `stock_candles_1m.source`)."""
    db.migrate(db_engine)
    with db_engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE stock_candles_1m DROP COLUMN source"))

    assert db.pending_migrations(db_engine) == ["stock_candles_1m.source 컬럼 없음"]


def test_migrate_and_pending_migrations_agree(db_engine):
    """둘이 다른 판정을 쓰면 '점검은 통과했는데 마이그레이션할 게 남았다'는
    조합이 생겨 점검이 무의미해진다. 그래서 `_drift()` 하나를 공유한다."""
    db.migrate(db_engine)
    with db_engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE stock_candles_1m DROP COLUMN source"))
        conn.execute(sa.text("ALTER TABLE stock_candles_1m DROP COLUMN trade_value"))

    pending = db.pending_migrations(db_engine)
    applied = db.migrate(db_engine)

    assert len(pending) == len(applied) == 2
    assert db.pending_migrations(db_engine) == []

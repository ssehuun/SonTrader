"""Trading-state schema and migration (구현 계획 1단계).

The system state lives in PostgreSQL, shared with the legacy kis_trading
collectors. This module owns the new tables from the implementation plan
(02-코드-구조.md §4) and applies additive adjusted-price columns to the
legacy ``stock_candles_1d`` table (0단계 검증에서 확인된 수정주가 결손 대응).

Schema principles from the plan:

- ``events`` is append-only; backtests replay by ``ingested_at``, never
  ``published_at`` (실전에서 잡을 수 없던 기회를 잡아버리는 look-ahead 방지).
- ``llm_judgments`` PK is (event_id, prompt_version, model) — one judgment
  per event, cacheable and reproducible. 재생성 불가능한 최우선 백업 대상.
- ``orders.idempotency_key`` is UNIQUE so duplicate submissions are blocked
  by the database, not by discipline.
- ``positions`` has no high_water column on purpose: it is derived state,
  recomputed from bars since entry (설계 6.5절).

All timestamps are naive KST wall-clock, matching KIS API payloads and the
legacy tables. Tests run against SQLite in-memory (JSON/JSONB variant), so
no database server is required.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection, Engine

metadata = MetaData()

# JSONB on PostgreSQL, generic JSON elsewhere (SQLite in tests).
_JSON = sa.JSON().with_variant(JSONB(), "postgresql")

events = Table(
    "events",
    metadata,
    Column("event_id", String(32), primary_key=True, comment="원천 고유 ID (DART 접수번호 등)"),
    Column("symbol", String(20), index=True, comment="종목코드 (비상장 관계사 공시 등은 NULL)"),
    Column("corp_code", String(20), comment="DART 고유번호"),
    Column("event_type", String(50), comment="공시 유형 (실적/유상증자/... 정규화된 분류)"),
    Column(
        "norm_key",
        String(200),
        index=True,
        comment="정정 접두어를 제거한 정규화 키 — 동일 이벤트 재진입 차단용",
    ),
    Column("title", Text),
    Column("published_at", DateTime, nullable=False, comment="공시 게시 시각 (KST)"),
    Column(
        "ingested_at",
        DateTime,
        nullable=False,
        index=True,
        comment="시스템 인지 시각 — 백테스트 재생 기준",
    ),
    Column("raw_json", _JSON),
)

llm_judgments = Table(
    "llm_judgments",
    metadata,
    Column("event_id", String(32), ForeignKey("events.event_id"), primary_key=True),
    Column("prompt_version", String(20), primary_key=True),
    Column("model", String(50), primary_key=True),
    Column("verdict", sa.Boolean, nullable=False, comment="진입 여부"),
    Column("confidence", Float, nullable=False, comment="확신도 0.0~1.0"),
    Column("exit_rule_json", _JSON, comment="진입 시점에 확정한 청산 조건"),
    Column("rationale", Text),
    Column("created_at", DateTime, nullable=False),
)

orders = Table(
    "orders",
    metadata,
    Column("order_id", String(36), primary_key=True, comment="내부 주문 ID (UUID)"),
    Column("idempotency_key", String(64), nullable=False, unique=True),
    Column("symbol", String(20), nullable=False),
    Column("side", String(4), nullable=False, comment="buy | sell"),
    Column("qty", Integer, nullable=False),
    Column("order_type", String(10), nullable=False, comment="market | limit"),
    Column("urgency", String(10), nullable=False, comment="IMMEDIATE | NEXT_OPEN"),
    Column(
        "status",
        String(20),
        nullable=False,
        index=True,
        comment="submitted/unknown/partial/filled/...",
    ),
    Column("event_id", String(32), ForeignKey("events.event_id"), comment="청산 주문은 NULL"),
    Column("broker_order_no", String(20), comment="KIS 주문번호(ODNO) — 미체결 조회·취소용"),
    Column("created_at", DateTime, nullable=False),
)

fills = Table(
    "fills",
    metadata,
    Column("fill_id", Integer, primary_key=True, autoincrement=True),
    Column("order_id", String(36), ForeignKey("orders.order_id"), nullable=False, index=True),
    Column("price", Integer, nullable=False, comment="체결가 (KRW)"),
    Column("qty", Integer, nullable=False),
    Column("ts", DateTime, nullable=False),
)

positions = Table(
    "positions",
    metadata,
    Column("symbol", String(20), primary_key=True),
    Column("qty", Integer, nullable=False),
    Column("avg_price", Numeric(16, 4), nullable=False, comment="부분체결 가중평균 진입가"),
    Column("entered_at", DateTime, nullable=False),
    Column("event_id", String(32), ForeignKey("events.event_id")),
    Column("exit_rule_json", _JSON, nullable=False, comment="진입 시 확정된 청산 조건"),
)

approvals = Table(
    "approvals",
    metadata,
    Column("proposal_id", String(36), primary_key=True),
    Column("payload_json", _JSON, nullable=False),
    Column("status", String(10), nullable=False, comment="pending/approved/rejected/expired"),
    Column("expires_at", DateTime, nullable=False, comment="TTL — 만료 시 폐기"),
)

# 레거시 일봉 테이블(kis_trading 소유)에 추가할 수정주가 관련 컬럼.
# 값 채우기는 2~3단계(수집기 보완)에서 한다; 여기서는 자리만 만든다.
CANDLES_1D_TABLE = "stock_candles_1d"
_CANDLES_1D_NEW_COLUMNS: dict[str, str] = {
    "flng_cls_code": "VARCHAR(2)",  # 락 구분 코드 (KIS flng_cls_code)
    "prtt_rate": "DOUBLE PRECISION",  # 분할 비율 (KIS prtt_rate)
    "mod_yn": "VARCHAR(1)",  # 변경 여부 (KIS mod_yn)
    "adj_factor": "DOUBLE PRECISION",  # 누적 수정계수 (자체 산출)
}


def get_engine(database_url: str) -> Engine:
    return sa.create_engine(database_url, pool_pre_ping=True)


def migrate(engine: Engine) -> list[str]:
    """Create missing tables/columns; return the actions performed.

    Idempotent and additive-only: rows are never touched, tables never
    dropped or rewritten, so the legacy kis_trading collectors keep working
    against the same database. Tables this module owns are column-synced —
    a column present in the metadata but missing in the database is added
    (nullable; tighten constraints manually if needed). This also surfaces
    a pre-existing same-name table with a foreign shape as explicit
    "added column" actions instead of a silent skip. Non-additive changes
    (type changes, drops, PK changes) require manual DDL.
    """
    inspector = sa.inspect(engine)
    before = set(inspector.get_table_names())
    actions: list[str] = []

    missing = [t for t in metadata.sorted_tables if t.name not in before]
    if missing:
        metadata.create_all(engine, tables=missing, checkfirst=False)
        actions += [f"created table {t.name}" for t in missing]

    with engine.begin() as conn:
        for table in metadata.sorted_tables:
            if table.name not in before:
                continue
            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name not in existing_cols:
                    ddl_type = col.type.compile(engine.dialect)
                    actions.append(_add_column(conn, table.name, col.name, ddl_type))
            # 인덱스도 동기화한다 — 컬럼만 추가하면 신규 DB(인덱스 포함 생성)와
            # 기존 DB(컬럼만 추가됨)의 스키마가 갈라진다.
            existing_idx = {ix["name"] for ix in inspector.get_indexes(table.name)}
            for index in table.indexes:
                if index.name not in existing_idx:
                    index.create(conn)
                    actions.append(f"created index {index.name}")

        if CANDLES_1D_TABLE in before:
            existing_cols = {c["name"] for c in inspector.get_columns(CANDLES_1D_TABLE)}
            for name, ddl_type in _CANDLES_1D_NEW_COLUMNS.items():
                if name not in existing_cols:
                    actions.append(_add_column(conn, CANDLES_1D_TABLE, name, ddl_type))

    return actions


def _add_column(conn: Connection, table_name: str, column_name: str, ddl_type: str) -> str:
    # Identifiers come from this module's table metadata and constants,
    # never from user input, so plain string DDL is safe here.
    conn.execute(sa.text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl_type}"))
    return f"added column {table_name}.{column_name}"

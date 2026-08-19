"""Trading-state schema and migration (구현 계획 1단계).

The system state lives in PostgreSQL (dedicated ``sontrader-db``; a legacy
kis_trading database also works — tables converge via additive sync). This
module owns the trading-state tables from the implementation plan
(02-코드-구조.md §4) plus the market-data tables the universe builder needs
(``symbol_master``, ``stock_candles_1d`` — 수정주가 컬럼 포함, 0단계 검증에서
확인된 수정주가 결손 대응).

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
    BigInteger,
    Boolean,
    Column,
    Date,
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
    Column(
        "status",
        String(10),
        nullable=False,
        comment="pending/approved/rejected/expired/consumed",
    ),
    Column("expires_at", DateTime, nullable=False, comment="TTL — 만료 시 폐기"),
)

# 킬 스위치 — 계좌가 하나뿐이라 전역 온/오프 단일 행이면 충분하다(id는 항상
# 'singleton'). 재시작 후에도 유지돼야 하므로 DB에 둔다(설계 2.5절).
kill_switch = Table(
    "kill_switch",
    metadata,
    Column("id", String(10), primary_key=True, comment="항상 'singleton'"),
    Column("engaged", sa.Boolean, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

# KOSPI/KOSDAQ 종목 마스터 (.mst 파일에서 적재). 플래그는 원본 문자
# ('Y'/'N' 등)를 그대로 보존한다 — 해석은 유니버스 필터(core)의 몫.
symbol_master = Table(
    "symbol_master",
    metadata,
    Column("symbol", String(20), primary_key=True),
    Column("name", String(100), nullable=False),
    Column("market", String(10), nullable=False, comment="KOSPI | KOSDAQ"),
    Column("group_code", String(2), comment="증권그룹 코드 (ST=주권)"),
    Column("cap_scale_code", String(1), comment="시가총액 규모 코드 ('0'=미분류)"),
    Column("low_liquidity_yn", String(1), comment="저유동성종목 여부"),
    Column("spac_yn", String(1), comment="SPAC(기업인수목적회사) 여부"),
    Column("pref_share_code", String(1), comment="우선주 구분 코드 ('0'=보통주)"),
    Column("base_price", Integer, comment="기준가"),
    Column("suspended_yn", String(1), comment="거래정지 여부"),
    Column("liquidation_yn", String(1), comment="정리매매 여부"),
    Column("managed_yn", String(1), comment="관리종목 여부"),
    Column("market_warning_code", String(2), comment="시장경고 코드 (''/'0'/'00'=정상)"),
    Column("unfaithful_yn", String(1), comment="불성실공시 여부"),
    Column("prev_volume", BigInteger, comment="전일 거래량"),
    Column("op_profit", BigInteger, comment="영업이익 (원본 단위 그대로)"),
    Column("market_cap", BigInteger, comment="시가총액 (억)"),
    # --- 구조적(시간 불변) 속성. 시변 플래그와 달리 과거 시점에 대해서도
    # 유효해서, 수집 단계 필터에 써도 생존/전방 편향이 생기지 않는다.
    Column("listing_date", Date, comment="상장일자 — 모멘텀 최소 이력 판정용"),
    Column("shares_outstanding", BigInteger, comment="상장주수"),
    Column("sector_large", String(4), comment="지수업종 대분류"),
    Column("sector_mid", String(4), comment="지수업종 중분류"),
    Column("sector_small", String(4), comment="지수업종 소분류"),
    Column("trading_unit", Integer, comment="매매수량단위 — 주문 수량이 이 배수여야 한다"),
    Column("updated_at", DateTime, nullable=False),
)

# 워치리스트 일별 스냅샷 — append-only point-in-time 기록 (설계 2.3절).
# 백테스트는 "그날의 워치리스트"를 이 테이블에서 읽는다 (재계산 금지).
watchlist_snapshots = Table(
    "watchlist_snapshots",
    metadata,
    Column("date", Date, primary_key=True),
    Column("symbol", String(20), primary_key=True),
    Column("score", Float, nullable=False, comment="모멘텀 점수"),
    Column("rank", Integer, nullable=False, comment="전체 후보 중 순위 (1이 최고)"),
)

# 일봉. 수정주가로 수집한다 (0단계에서 확인한 레거시 수집기의 원주가 문제 대응).
# 기업행위 발생 시 과거분이 소급 수정되므로 수집기가 겹침 구간을 대조해
# 불일치 시 전체 재수집한다 (data/prices.py). 레거시 kis_trading DB를 쓰는
# 경우에도 아래 정의 기준으로 컬럼이 additive 동기화된다.
stock_candles_1d = Table(
    "stock_candles_1d",
    metadata,
    Column("symbol", String(20), primary_key=True),
    Column("date", Date, primary_key=True),
    Column("open", Integer),
    Column("high", Integer),
    Column("low", Integer),
    Column("close", Integer),
    Column("volume", BigInteger),
    Column("trade_value", BigInteger, comment="누적 거래대금"),
    # 레거시 kis_trading 수집기와 같은 DB를 쓸 때 그쪽 INSERT가 깨지지 않도록
    # 레거시 컬럼도 정의해 둔다 (우리는 쓰지 않음).
    Column("prdy_vrss", Integer, comment="전일 대비 (레거시 호환)"),
    Column("prdy_ctrt", Float, comment="전일 대비율 (레거시 호환)"),
    Column("flng_cls_code", String(2), comment="락 구분 코드"),
    Column("prtt_rate", Float, comment="분할 비율"),
    Column("mod_yn", String(1), comment="변경 여부"),
    Column("adj_factor", Float, comment="누적 수정계수 — 수정주가 수집이라 현재 미사용(NULL)"),
)

# 1분봉 — 웹소켓 실시간체결가(adapters/live_ws.py)가 집계해 채운다.
# stock_candles_1d와 별개 테이블이다: PK가 date가 아니라 ts(분 단위)이고,
# 재연결 시 같은 분이 다시 집계될 수 있어 upsert로 쓴다(data/live_bars.py).
stock_candles_1m = Table(
    "stock_candles_1m",
    metadata,
    Column("symbol", String(20), primary_key=True),
    Column("ts", DateTime, primary_key=True),
    Column("open", Integer, nullable=False),
    Column("high", Integer, nullable=False),
    Column("low", Integer, nullable=False),
    Column("close", Integer, nullable=False),
    Column("volume", BigInteger, nullable=False),
)

# 국내휴장일조회(CTCA0903R) 캐시 — KIS가 "가급적 1일 1회 호출"을 명시
# 요청해서(원장서비스 영향) 매번 조회하지 않고 여기 저장해 재사용한다
# (data/calendar.py).
market_calendar = Table(
    "market_calendar",
    metadata,
    Column("date", Date, primary_key=True),
    Column(
        "open_yn", String(1), nullable=False, comment="개장일여부(opnd_yn) — 주문 가능 여부 기준"
    ),
    Column("business_day_yn", String(1), nullable=False, comment="영업일여부(bzdy_yn)"),
    Column("trading_day_yn", String(1), nullable=False, comment="거래일여부(tr_day_yn)"),
    Column("settlement_day_yn", String(1), nullable=False, comment="결제일여부(sttl_day_yn)"),
)


def get_engine(database_url: str) -> Engine:
    return sa.create_engine(database_url, pool_pre_ping=True)


def upsert_rows(
    conn: Connection,
    table: Table,
    rows: list[dict],
    *,
    key_cols: tuple[str, ...],
    ignore_conflicts: bool = False,
) -> None:
    """Dialect-aware chunked upsert, shared by all collectors.

    Chunk size is derived from the column count so a statement never exceeds
    SQLite's ~999 bind-parameter limit. ``ignore_conflicts=True`` keeps
    existing rows (ON CONFLICT DO NOTHING — append-only ingestion);
    otherwise conflicting rows are updated in place.
    """
    if not rows:
        return
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert

    chunk_size = max(1, 900 // len(table.columns))
    for start in range(0, len(rows), chunk_size):
        stmt = insert(table).values(rows[start : start + chunk_size])
        if ignore_conflicts:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(key_cols))
        else:
            update_cols = {
                c.name: getattr(stmt.excluded, c.name)
                for c in table.columns
                if c.name not in key_cols
            }
            stmt = stmt.on_conflict_do_update(index_elements=list(key_cols), set_=update_cols)
        conn.execute(stmt)


cycle_log = Table(
    "cycle_log",
    metadata,
    Column("ts", DateTime, primary_key=True, comment="사이클 시각 (KST naive)"),
    Column("watchlist_n", Integer, nullable=False),
    Column("positions_n", Integer, nullable=False),
    Column("cash", BigInteger, nullable=False),
    Column("equity", BigInteger, nullable=False),
    Column("pending_n", Integer, nullable=False, comment="승인 대기 중인 제안 수"),
    Column("orders_n", Integer, nullable=False, comment="이번 사이클에 제출한 주문 수"),
    Column(
        "rejections",
        _JSON,
        comment="게이트가 스킵한 신규 진입 [{symbol, reason, event_id}]. '왜 안 샀나'의 근거",
    ),
    Column(
        "halted",
        Boolean,
        nullable=False,
        comment="reconcile 불일치로 매매를 중단했는가",
    ),
)


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

    return actions


def _add_column(conn: Connection, table_name: str, column_name: str, ddl_type: str) -> str:
    # Identifiers come from this module's table metadata and constants,
    # never from user input, so plain string DDL is safe here.
    conn.execute(sa.text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl_type}"))
    return f"added column {table_name}.{column_name}"

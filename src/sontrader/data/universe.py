"""워치리스트 스냅샷 빌더 (구현 계획 3단계) — 순수 core를 DB 데이터로 조립.

파이프라인 (일 1회, **장 마감 후** 실행 전제 — 장중에 실행하면 오늘의 임시
종가가 점수에 섞여 재현 불가능한 스냅샷이 된다):

1. symbol_master → 방어 필터 (core.filters) → 후보 종목.
   `UniverseScope`가 무엇으로 거를지 정한다 — 오늘 매매용은 시변 상태까지
   보는 `is_tradeable()`, 과거 소급 생성은 구조적 속성만 보는
   `is_collectable()`. 후자를 쓰는 이유는 그 enum의 독스트링 참고.
2. 스냅샷 기준일 결정: 요청일 이하의 **마지막 거래일** (저장된 일봉의 최신
   날짜). 벽시계 날짜가 아니라 데이터 날짜로 기록해야 주말/휴장일 실행이
   point-in-time 기록을 오염시키지 않고, 수집이 밀린 날은 밀린 날짜로
   정직하게 남는다.
3. stock_candles_1d에서 기준일 이하의 종가·거래대금·거래량 로드 (룩백 하한까지만)
4. 신선도 게이트: 마지막 봉이 기준일에서 RECENCY_LIMIT_DAYS보다 오래된
   종목은 제외 (상장폐지·수집 실패 종목이 옛 점수로 순위에 남는 것 방지)
5. 거래정지 필터: 최근 20거래일에 거래량 0인 날이 있으면 제외.
   **4번이 이걸 잡지 못한다** — KIS는 정지일에도 봉을 준다(거래량 0, OHLC는
   직전 종가). 그래서 시계열이 멈추지 않고 신선도 게이트가 영영 발동하지
   않는다. 실측: 전체 봉의 3.2%, 2,463종목 중 433종목이 해당.
6. 유동성 필터: 최근 20거래일 평균 거래대금 하한 (파라미터 — 설계 8절).
   5번과 겹쳐 보이지만 부족하다 — 20일 중 1~2일 정지는 평균을 10%쯤 낮출
   뿐이라 하한을 통과한다.
7. 모멘텀 점수 (core.momentum) — 이력이 부족한 신규 상장은 자연 탈락
8. 직전 스냅샷과 히스테리시스 (core.watchlist, 편입 50/이탈 70)
9. watchlist_snapshots에 저장 — 같은 기준일 재실행이면 그날 행을 교체하므로,
   입력 데이터가 같으면 결과도 같다 (검증 요건)

이 필터는 **진입**만 막는다. 보유 중 정지된 종목의 청산은 체결 계층
(`adapters/broker_sim.py`)이 다룬다 — 정지일 봉으로는 체결시키지 않는다.

읽기는 저장된 캔들만 사용하므로 API 호출이 없다. 참고: 계획서 구조상 이
조립 계층은 apps/에 속하지만, apps/가 생기는 단계(5단계 백테스트)까지는
data/에 둔다 — 의도된 잠정 배치.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.core.filters import (
    HALT_LOOKBACK_BARS,
    SecurityInfo,
    StructuralInfo,
    has_recent_halt,
    is_collectable,
    is_tradeable,
)
from sontrader.core.momentum import LOOKBACK_BARS, SKIP_BARS, momentum_score
from sontrader.core.watchlist import ENTER_RANK, EXIT_RANK, WatchlistEntry, build_watchlist
from sontrader.data import db

MIN_AVG_TRADE_VALUE = 1_000_000_000  # 최근 20거래일 평균 거래대금 하한 (10억 KRW)
LIQUIDITY_BARS = 20
RECENCY_LIMIT_DAYS = 7  # 마지막 봉이 기준일보다 이보다 오래되면 점수 제외 (달력일)
_SYMBOL_CHUNK = 500  # IN 절 바인드 변수 한도(SQLite ~999) 대비


class UniverseScope(Enum):
    """1번 단계(마스터 필터)에서 무엇으로 거를지.

    `symbol_master`는 **오늘자 스냅샷**이다. 그래서 과거 날짜의 스냅샷을
    지금 만들 때 어떤 필드를 쓰느냐에 따라 편향의 방향이 달라진다.
    """

    TRADEABLE_NOW = "tradeable_now"
    """`is_tradeable()` — 시변 상태(관리종목·거래정지·시장경고·영업이익·
    시총규모·기준가)까지 본다. 오늘 매매할 유니버스를 뽑는 데는 이게 맞다."""

    STRUCTURAL = "structural"
    """`is_collectable()` — 구조적 속성(증권 종류·우선주·SPAC·상장일자)만
    본다. **과거 스냅샷을 소급 생성할 때 쓴다.**

    오늘의 상태 플래그를 과거에 적용하면 "오늘 관리종목인 회사는 2019년에도
    제외"가 되어 확정적인 생존 편향이 들어간다 — 미래 정보를 쓰는 것이라
    되돌릴 수 없다. 반면 구조적 필터만 쓰면 그 시점에 실제로 부실했던 종목이
    후보에 남는데, 그 실질적 영향(거래 안 됨·거래대금 급감)은 뒤따르는
    거래정지 필터(`volume==0`)와 유동성 필터가 **그 시점의 사실로** 잡아낸다.

    상장일자 판정은 `as_of` 기준이라 이 모드가 오히려 더 point-in-time에
    가깝다 — `is_tradeable()`에는 날짜 개념이 아예 없다."""


class UniverseError(RuntimeError):
    """스냅샷을 만들 수 없는 상태 (예: 일봉 데이터 없음)."""


@dataclass(frozen=True)
class SnapshotResult:
    as_of: date  # 스냅샷이 기록된 기준일 (마지막 거래일)
    requested: date  # 호출자가 요청한 날짜
    entries: list[WatchlistEntry]
    candidates: int  # 마스터 필터 통과 종목 수
    scored: int  # 모멘텀 점수가 산출된 종목 수 (신선도·정지·유동성 통과)
    halted: int = 0  # 최근 거래정지로 제외된 종목 수


def build_snapshot(
    engine: Engine,
    *,
    as_of: date,
    enter_rank: int = ENTER_RANK,
    exit_rank: int = EXIT_RANK,
    lookback: int = LOOKBACK_BARS,
    skip: int = SKIP_BARS,
    min_avg_trade_value: int = MIN_AVG_TRADE_VALUE,
    halt_lookback: int = HALT_LOOKBACK_BARS,
    scope: UniverseScope = UniverseScope.TRADEABLE_NOW,
) -> SnapshotResult:
    effective = _effective_date(engine, as_of)
    if effective is None:
        raise UniverseError(
            "no daily candles at or before the requested date — run `sontrader collect-prices`"
        )
    candidates = _load_candidates(engine, as_of=effective, scope=scope)
    series = _load_series(engine, candidates, effective, lookback)

    scores: dict[str, float] = {}
    halted = 0
    for symbol, s in series.items():
        if s.last_bar is None or (effective - s.last_bar).days > RECENCY_LIMIT_DAYS:
            continue  # 상장폐지·수집 실패로 시계열이 멈춘 종목
        # 거래정지는 신선도 게이트에 안 걸린다 — 정지일에도 봉이 오기 때문이다.
        if has_recent_halt(s.volumes, bars=halt_lookback):
            halted += 1
            continue
        if not _is_liquid(s.trade_values, min_avg_trade_value):
            continue
        score = momentum_score(s.closes, lookback=lookback, skip=skip)
        if score is not None:
            scores[symbol] = score

    previous = _previous_watchlist(engine, effective)
    entries = build_watchlist(scores, previous, enter_rank=enter_rank, exit_rank=exit_rank)
    _store_snapshot(engine, effective, entries)
    return SnapshotResult(
        as_of=effective,
        requested=as_of,
        entries=entries,
        candidates=len(candidates),
        scored=len(scores),
        halted=halted,
    )


def _load_candidates(engine: Engine, *, as_of: date, scope: UniverseScope) -> list[str]:
    """마스터 필터를 통과한 후보 종목. 컬럼 목록은 dataclass 필드에서
    파생하므로 스키마와 어긋날 수 없다."""
    info_type = SecurityInfo if scope is UniverseScope.TRADEABLE_NOW else StructuralInfo
    columns = [db.symbol_master.c[field.name] for field in dataclasses.fields(info_type)]
    with engine.connect() as conn:
        rows = conn.execute(sa.select(*columns)).all()
    if scope is UniverseScope.TRADEABLE_NOW:
        return [row.symbol for row in rows if is_tradeable(SecurityInfo(**row._mapping))]
    return [
        row.symbol for row in rows if is_collectable(StructuralInfo(**row._mapping), today=as_of)
    ]


def _effective_date(engine: Engine, as_of: date) -> date | None:
    """요청일 이하의 마지막 거래일 = 저장된 일봉의 최신 날짜."""
    columns = db.stock_candles_1d.c
    with engine.connect() as conn:
        return conn.execute(
            sa.select(sa.func.max(columns.date)).where(columns.date <= as_of)
        ).scalar_one()


@dataclasses.dataclass
class _Series:
    """한 종목의 시계열 (날짜 오름차순)."""

    closes: list[float] = dataclasses.field(default_factory=list)
    trade_values: list[float] = dataclasses.field(default_factory=list)
    volumes: list[int | None] = dataclasses.field(default_factory=list)
    last_bar: date | None = None


def _load_series(
    engine: Engine, symbols: list[str], as_of: date, lookback: int
) -> dict[str, _Series]:
    """종목별 시계열 — 날짜 오름차순, 룩백 하한까지만.

    하한을 두는 이유: 이력이 무한히 쌓여도 모멘텀은 마지막 lookback+1개만
    쓴다. 거래일→달력일 여유 환산(×1.7 + 40)으로 휴장을 흡수한다.

    거래량도 함께 읽는다 — 거래정지일을 가려내는 유일한 근거다
    (`core.filters.has_recent_halt` 참고).
    """
    if not symbols:
        return {}
    lower_bound = as_of - timedelta(days=int(lookback * 1.7) + 40)
    columns = db.stock_candles_1d.c
    series: dict[str, _Series] = {}
    with engine.connect() as conn:
        for start in range(0, len(symbols), _SYMBOL_CHUNK):
            chunk = symbols[start : start + _SYMBOL_CHUNK]
            result = conn.execute(
                sa.select(
                    columns.symbol,
                    columns.date,
                    columns.close,
                    columns.trade_value,
                    columns.volume,
                )
                .where(
                    columns.symbol.in_(chunk),
                    columns.date <= as_of,
                    columns.date >= lower_bound,
                )
                .order_by(columns.symbol, columns.date)
            )
            for symbol, day, close, trade_value, volume in result:
                entry = series.setdefault(symbol, _Series())
                entry.closes.append(close)
                entry.trade_values.append(trade_value)
                entry.volumes.append(volume)
                entry.last_bar = day
    return series


def _is_liquid(trade_values: list[float], min_avg_trade_value: int) -> bool:
    recent = [v for v in trade_values[-LIQUIDITY_BARS:] if v is not None]
    if not recent:
        return False
    return sum(recent) / len(recent) >= min_avg_trade_value


def _previous_watchlist(engine: Engine, as_of: date) -> set[str]:
    """as_of 직전 스냅샷의 종목들 (같은 날 재실행이 자기 자신을 참조하지 않게 미만 조건)."""
    columns = db.watchlist_snapshots.c
    with engine.connect() as conn:
        latest = conn.execute(
            sa.select(sa.func.max(columns.date)).where(columns.date < as_of)
        ).scalar_one()
        if latest is None:
            return set()
        rows = conn.execute(sa.select(columns.symbol).where(columns.date == latest)).all()
    return {row.symbol for row in rows}


def _store_snapshot(engine: Engine, as_of: date, entries: list[WatchlistEntry]) -> None:
    columns = db.watchlist_snapshots.c
    with engine.begin() as conn:
        # 같은 기준일 재실행은 그날 행을 교체한다. 과거 날짜는 절대 건드리지
        # 않으므로 point-in-time 기록이라는 성질은 유지된다.
        conn.execute(sa.delete(db.watchlist_snapshots).where(columns.date == as_of))
        if entries:
            conn.execute(
                db.watchlist_snapshots.insert(),
                [
                    {"date": as_of, "symbol": e.symbol, "score": e.score, "rank": e.rank}
                    for e in entries
                ],
            )

"""워치리스트 스냅샷 빌더 (구현 계획 3단계) — 순수 core를 DB 데이터로 조립.

파이프라인 (일 1회, **장 마감 후** 실행 전제 — 장중에 실행하면 오늘의 임시
종가가 점수에 섞여 재현 불가능한 스냅샷이 된다):

1. symbol_master → 방어 필터 (core.filters) → 후보 종목
2. 스냅샷 기준일 결정: 요청일 이하의 **마지막 거래일** (저장된 일봉의 최신
   날짜). 벽시계 날짜가 아니라 데이터 날짜로 기록해야 주말/휴장일 실행이
   point-in-time 기록을 오염시키지 않고, 수집이 밀린 날은 밀린 날짜로
   정직하게 남는다.
3. stock_candles_1d에서 기준일 이하의 종가·거래대금 로드 (룩백 하한까지만)
4. 신선도 게이트: 마지막 봉이 기준일에서 RECENCY_LIMIT_DAYS보다 오래된
   종목은 제외 (상장폐지·수집 실패 종목이 옛 점수로 순위에 남는 것 방지)
5. 유동성 필터: 최근 20거래일 평균 거래대금 하한 (파라미터 — 설계 8절)
6. 모멘텀 점수 (core.momentum) — 이력이 부족한 신규 상장은 자연 탈락
7. 직전 스냅샷과 히스테리시스 (core.watchlist, 편입 50/이탈 70)
8. watchlist_snapshots에 저장 — 같은 기준일 재실행이면 그날 행을 교체하므로,
   입력 데이터가 같으면 결과도 같다 (검증 요건)

읽기는 저장된 캔들만 사용하므로 API 호출이 없다. 참고: 계획서 구조상 이
조립 계층은 apps/에 속하지만, apps/가 생기는 단계(5단계 백테스트)까지는
data/에 둔다 — 의도된 잠정 배치.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date, timedelta

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.core.filters import SecurityInfo, is_tradeable
from sontrader.core.momentum import LOOKBACK_BARS, SKIP_BARS, momentum_score
from sontrader.core.watchlist import ENTER_RANK, EXIT_RANK, WatchlistEntry, build_watchlist
from sontrader.data import db

MIN_AVG_TRADE_VALUE = 1_000_000_000  # 최근 20거래일 평균 거래대금 하한 (10억 KRW)
LIQUIDITY_BARS = 20
RECENCY_LIMIT_DAYS = 7  # 마지막 봉이 기준일보다 이보다 오래되면 점수 제외 (달력일)
_SYMBOL_CHUNK = 500  # IN 절 바인드 변수 한도(SQLite ~999) 대비


class UniverseError(RuntimeError):
    """스냅샷을 만들 수 없는 상태 (예: 일봉 데이터 없음)."""


@dataclass(frozen=True)
class SnapshotResult:
    as_of: date  # 스냅샷이 기록된 기준일 (마지막 거래일)
    requested: date  # 호출자가 요청한 날짜
    entries: list[WatchlistEntry]
    candidates: int  # 마스터 필터 통과 종목 수
    scored: int  # 모멘텀 점수가 산출된 종목 수 (신선도·유동성 통과)


def build_snapshot(
    engine: Engine,
    *,
    as_of: date,
    enter_rank: int = ENTER_RANK,
    exit_rank: int = EXIT_RANK,
    lookback: int = LOOKBACK_BARS,
    skip: int = SKIP_BARS,
    min_avg_trade_value: int = MIN_AVG_TRADE_VALUE,
) -> SnapshotResult:
    candidates = _load_tradeable_symbols(engine)
    effective = _effective_date(engine, as_of)
    if effective is None:
        raise UniverseError(
            "no daily candles at or before the requested date — run `sontrader collect-prices`"
        )
    series = _load_series(engine, candidates, effective, lookback)

    scores: dict[str, float] = {}
    for symbol, (closes, trade_values, last_bar) in series.items():
        if (effective - last_bar).days > RECENCY_LIMIT_DAYS:
            continue  # 상장폐지·수집 실패로 시계열이 멈춘 종목
        if not _is_liquid(trade_values, min_avg_trade_value):
            continue
        score = momentum_score(closes, lookback=lookback, skip=skip)
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
    )


def _load_tradeable_symbols(engine: Engine) -> list[str]:
    # 컬럼 목록은 SecurityInfo 필드에서 파생 — 둘이 어긋날 수 없다.
    columns = [db.symbol_master.c[field.name] for field in dataclasses.fields(SecurityInfo)]
    with engine.connect() as conn:
        rows = conn.execute(sa.select(*columns)).all()
    return [row.symbol for row in rows if is_tradeable(SecurityInfo(**row._mapping))]


def _effective_date(engine: Engine, as_of: date) -> date | None:
    """요청일 이하의 마지막 거래일 = 저장된 일봉의 최신 날짜."""
    columns = db.stock_candles_1d.c
    with engine.connect() as conn:
        return conn.execute(
            sa.select(sa.func.max(columns.date)).where(columns.date <= as_of)
        ).scalar_one()


def _load_series(
    engine: Engine, symbols: list[str], as_of: date, lookback: int
) -> dict[str, tuple[list[float], list[float], date]]:
    """종목별 (종가, 거래대금, 마지막 봉 날짜) — 날짜 오름차순, 룩백 하한까지만.

    하한을 두는 이유: 이력이 무한히 쌓여도 모멘텀은 마지막 lookback+1개만
    쓴다. 거래일→달력일 여유 환산(×1.7 + 40)으로 휴장을 흡수한다.
    """
    if not symbols:
        return {}
    lower_bound = as_of - timedelta(days=int(lookback * 1.7) + 40)
    columns = db.stock_candles_1d.c
    series: dict[str, tuple[list[float], list[float], date]] = {}
    with engine.connect() as conn:
        for start in range(0, len(symbols), _SYMBOL_CHUNK):
            chunk = symbols[start : start + _SYMBOL_CHUNK]
            result = conn.execute(
                sa.select(columns.symbol, columns.date, columns.close, columns.trade_value)
                .where(
                    columns.symbol.in_(chunk),
                    columns.date <= as_of,
                    columns.date >= lower_bound,
                )
                .order_by(columns.symbol, columns.date)
            )
            for symbol, day, close, trade_value in result:
                closes, trade_values, _ = series.get(symbol, ([], [], day))
                closes.append(close)
                trade_values.append(trade_value)
                series[symbol] = (closes, trade_values, day)
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

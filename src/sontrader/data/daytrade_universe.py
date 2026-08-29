"""데이트레이딩 감시 대상 — D-1 거래량 상위 (R31).

## 규칙

    감시 대상 = D-1 거래대금 하한 이상 중 D-1 거래량 상위 N
    장전에 확정하고 그날 하루 유지한다.

**장중 재계산이 없다.** 순위 API도, 폴링도, 예열 구독도 쓰지 않는다 —
필요한 것이 `stock_candles_1d`에 이미 있다.

## 왜 거래량인가 (2026-08-27 실측)

상위 36 중 그날 +7% 도달 비율. D-1 순위이므로 선행참조가 없다:

| 정렬 | 리서처 (2,117 거래일) | 팀리드 독립검증 (2024~) |
|---|---|---|
| **거래량** | **10.62%** | **33.48%** |
| 거래증가율 | 9.69% | 31.21% |
| 거래대금 | 6.79% | 19.59% |
| 등락률 | 6.22% | **12.93%** |
| 무작위 | 3.83% | **12.93%** |

**등락률은 무작위와 구별되지 않는다.** 원래 후보였는데 실측이 뒤집었다.

## 왜 하필 36인가

웹소켓 실시간 등록 한도가 **41**이다(2026-08-27 실측, `docs/system/03-운영.md`
T18). 보유 포지션 자리를 남겨야 한다:

    36 = 한도 41 − 최대 동시보유 5

**보유 종목은 절대 구독 해제하지 않는다**(T30). 해제하면 그 종목의 봉이 안
쌓이고 → 청산 판정 입력이 비고 → 스톱이 영영 안 걸린다. 그래서 보유가
생기면 후보가 밀려나지, 보유가 밀려나지 않는다.

값은 주입 가능하다 — 한도가 계정 등급에 따라 다를 수 있고(T18), 2연결이
허용되면 상한이 달라진다.

## 유동성 하한을 왜 따로 두나

거래량만 보면 **저가 대량 거래**가 순위를 채운다. 100원짜리가 1,000만 주
거래돼도 거래대금은 10억이다. 진입 물량(종목당 200만원)을 소화할 수 없는
종목이 감시 대상을 먹으면 슬롯만 낭비한다.

## 스냅샷은 남기고 다시 계산하지 않는다

`daytrade_watchlist_snapshots`에 저장한다. 사후 재계산하면 그날 몰랐던 정보가
섞인다 — 일봉은 수정주가라 기업행위 시점에 과거가 소급 변경되므로, 나중에
같은 쿼리를 돌려도 **그날과 다른 답이 나온다**(01문서 §5.2, 리서처 R19).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.data import db

log = logging.getLogger(__name__)

# 웹소켓 실시간 등록 한도 (2026-08-27 실측 — `docs/system/03-운영.md` T18 2번).
# 45종목을 요청해 41이 성공하고 42~45번째가 `OPSP0008 MAX SUBSCRIBE OVER`로
# 거부됐다. **상수로 믿지 않는다** — 계정 등급·TR 종류에 따라 다를 수 있어
# 호출자가 바꿀 수 있게 열어 둔다.
WS_SUBSCRIBE_LIMIT = 41

# 최대 동시보유 종목 수 (`core/gate.py`의 `GateConfig.max_positions` 기본값).
# 여기서 다시 적는 이유는 감시 슬롯 예산이 그 값에 직접 걸리기 때문이다 —
# 게이트 설정을 바꾸면 이 값도 함께 봐야 한다.
RESERVED_FOR_POSITIONS = 5

# 후보 슬롯. 이 유도를 지우지 말 것 — 36이라는 숫자만 남으면 왜 36인지 모른다.
DEFAULT_TOP_N = WS_SUBSCRIBE_LIMIT - RESERVED_FOR_POSITIONS  # 41 − 5 = 36

# 유동성 하한 (D-1 거래대금). 저가 대량 거래가 순위를 채우는 것을 막는다.
# **백테스트로 확정할 값이 아니라 집행 제약에서 온 값이다** — 종목당 200만원을
# 스프레드를 크게 벌리지 않고 넣을 수 있는가가 기준이고, 10억은 그 최소선으로
# 팀리드가 정했다. 실측으로 다시 볼 여지가 있다.
DEFAULT_MIN_TRADE_VALUE = 1_000_000_000


class DaytradeUniverseError(RuntimeError):
    """감시 대상을 만들 수 없는 상태 (예: D-1 일봉이 없음)."""


@dataclass(frozen=True)
class WatchEntry:
    symbol: str
    rank: int  # 1이 거래량 최대
    volume: int
    trade_value: int | None


@dataclass(frozen=True)
class DaytradeSnapshot:
    """`as_of` 하루치 감시 대상. `source_date`가 근거로 쓴 D-1이다."""

    date: date
    source_date: date
    entries: tuple[WatchEntry, ...]

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(e.symbol for e in self.entries)


def select(
    rows: Sequence[tuple[str, int, int | None]],
    *,
    min_trade_value: int = DEFAULT_MIN_TRADE_VALUE,
    top_n: int = DEFAULT_TOP_N,
) -> list[WatchEntry]:
    """(종목, 거래량, 거래대금) → 감시 대상. **순수 함수 — DB도 시각도 모른다.**

    유동성 하한을 먼저 걸고 거래량 내림차순으로 자른다. 순서가 중요하다 —
    자른 뒤 거르면 하한에 걸린 종목이 슬롯을 먹고 사라져 `top_n`보다 적게 남는다.

    거래량이 같으면 **거래대금이 큰 쪽**, 그것도 같으면 종목코드 순이다.
    같은 입력에 같은 답이 나와야 스냅샷을 재현할 수 있다.

    `trade_value`가 `None`인 행은 **버린다.** 하한을 통과했는지 알 수 없는데
    통과시키면 그게 곧 하한을 없애는 것이다 — 판정할 수 없으면 넣지 않는다.
    """
    if top_n < 1:
        raise ValueError(f"top_n must be >= 1: {top_n}")
    if min_trade_value < 0:
        raise ValueError(f"min_trade_value must be >= 0: {min_trade_value}")

    liquid = [
        (symbol, volume, trade_value)
        for symbol, volume, trade_value in rows
        if trade_value is not None and trade_value >= min_trade_value and volume is not None
    ]
    liquid.sort(key=lambda r: (-r[1], -(r[2] or 0), r[0]))
    return [
        WatchEntry(symbol=symbol, rank=index, volume=volume, trade_value=trade_value)
        for index, (symbol, volume, trade_value) in enumerate(liquid[:top_n], start=1)
    ]


def build_snapshot(
    engine: Engine,
    *,
    as_of: date,
    min_trade_value: int = DEFAULT_MIN_TRADE_VALUE,
    top_n: int = DEFAULT_TOP_N,
    store: bool = True,
) -> DaytradeSnapshot:
    """`as_of` 거래일의 감시 대상을 D-1 일봉에서 만든다.

    **D-1은 달력상 어제가 아니라 `as_of` 미만의 가장 최근 일봉 날짜**다.
    휴일·주말을 캘린더 없이 넘기는 방법이고, `data/universe.py`의
    `_effective_date`와 같은 규약이다.

    같은 날 재실행하면 **저장된 스냅샷을 그대로 돌려준다** — 다시 계산하지
    않는다(R19). 일봉은 수정주가라 기업행위 시점에 과거가 소급 변경되므로,
    재계산하면 그날과 다른 답이 나온다.
    """
    stored = load_snapshot(engine, as_of)
    if stored is not None:
        log.info(
            "%s 감시 대상은 이미 저장돼 있다 (%d종목) — 재계산하지 않는다",
            as_of,
            len(stored.entries),
        )
        return stored

    source_date = _previous_trading_date(engine, as_of)
    if source_date is None:
        raise DaytradeUniverseError(
            f"{as_of} 이전 일봉이 없다 — `sontrader collect-prices`를 먼저 돌려야 한다"
        )

    columns = db.stock_candles_1d.c
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(columns.symbol, columns.volume, columns.trade_value).where(
                columns.date == source_date
            )
        ).all()

    entries = select(
        [(r.symbol, r.volume, r.trade_value) for r in rows],
        min_trade_value=min_trade_value,
        top_n=top_n,
    )
    if not entries:
        raise DaytradeUniverseError(
            f"{source_date} 일봉에서 유동성 하한 {min_trade_value:,}원을 넘는 종목이 없다"
        )

    snapshot = DaytradeSnapshot(date=as_of, source_date=source_date, entries=tuple(entries))
    if store:
        _store(engine, snapshot)
    return snapshot


def load_snapshot(engine: Engine, as_of: date) -> DaytradeSnapshot | None:
    """저장된 감시 대상. 없으면 None.

    **실전은 이걸 읽지 다시 계산하지 않는다.** 장중에 재계산하면 그날 감시
    대상이 사이클마다 달라져, 붙였다 뗐다 하는 구독이 생긴다.
    """
    columns = db.daytrade_watchlist_snapshots.c
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                columns.symbol,
                columns.rank,
                columns.volume,
                columns.trade_value,
                columns.source_date,
            )
            .where(columns.date == as_of)
            .order_by(columns.rank)
        ).all()
    if not rows:
        return None
    return DaytradeSnapshot(
        date=as_of,
        source_date=rows[0].source_date,
        entries=tuple(
            WatchEntry(symbol=r.symbol, rank=r.rank, volume=r.volume, trade_value=r.trade_value)
            for r in rows
        ),
    )


def _previous_trading_date(engine: Engine, as_of: date) -> date | None:
    """`as_of` **미만**의 가장 최근 일봉 날짜. 휴일 캘린더를 알 필요가 없다."""
    columns = db.stock_candles_1d.c
    with engine.connect() as conn:
        return conn.execute(
            sa.select(sa.func.max(columns.date)).where(columns.date < as_of)
        ).scalar_one_or_none()


def _store(engine: Engine, snapshot: DaytradeSnapshot) -> None:
    with engine.begin() as conn:
        db.upsert_rows(
            conn,
            db.daytrade_watchlist_snapshots,
            [
                {
                    "date": snapshot.date,
                    "symbol": e.symbol,
                    "rank": e.rank,
                    "volume": e.volume,
                    "trade_value": e.trade_value,
                    "source_date": snapshot.source_date,
                }
                for e in snapshot.entries
            ],
            key_cols=("date", "symbol"),
        )

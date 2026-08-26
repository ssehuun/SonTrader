"""실측 슬리피지 추출기 — 백테스트를 실전에 붙이는 유일한 피드백 루프.

`SimBrokerConfig.slippage_bps`는 지금 **10bp 자리표시자**다(01문서 §8의
미확정 파라미터). 백테스트 안에서만 검증하면 영원히 자기 가정을 확인할
뿐이므로, 실제로 나간 주문의 "의사결정 기준가 대비 체결가"를 뽑아 그
자리표시자를 대체할 근거로 삼는다.

## 무엇을 재는가 — 슬리피지가 아니라 **집행 손실(implementation shortfall)**이다

`orders.ref_price`는 그 주문을 만든 사이클이 본 마지막 완성 봉의 종가다
(`core/diff.py`). 체결은 그 뒤에 일어나므로, 여기서 나오는 bp에는 셋이
섞여 있다:

1. 순수 슬리피지 (호가 스프레드·시장 충격)
2. 의사결정 시점과 체결 시점 사이의 **가격 변동**(NEXT_OPEN 진입이면 밤샘 갭)
3. 수수료·세금은 **포함되지 않는다** — 그건 별도 파라미터로 이미 확정됐다

**2번을 분리할 수 없다.** 분리하려면 주문을 낸 그 순간의 호가가 필요한데
우리는 저장하지 않는다. 그래서 이 값을 그대로 `slippage_bps`에 넣으면
갭까지 비용으로 이중 계상된다.

**올바른 사용법은 같은 통계를 백테스트에서도 뽑아 비교하는 것이다.**
백테스트에도 같은 2번(밤샘 갭)이 들어 있으므로, 두 분포의 **차이**가
곧 모델이 놓치고 있는 실제 슬리피지다. 그래서 이 모듈은 `orders`/`fills`와
`BacktestResult` 양쪽에서 같은 함수로 통계를 낸다.

## 부호 규약

항상 **불리한 방향이 양수(+)**다. 매수는 비싸게 샀을 때, 매도는 싸게
팔았을 때 +bp. 부호를 side마다 뒤집지 않으면 매수·매도를 한 표본으로
합칠 수 없고, "평균 슬리피지"라는 말 자체가 성립하지 않는다.

## 표본이 없으면 숫자를 만들지 않는다

`SlippageStats.sample_size == 0`이면 모든 통계가 None이다. 0.0을 반환하면
"슬리피지가 없다"로 읽혀 자리표시자보다 나쁜 거짓말이 된다.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.core.types import Side
from sontrader.data import db


@dataclass(frozen=True)
class SlippageSample:
    """체결 하나의 집행 손실. `bps`는 불리한 방향이 양수다."""

    symbol: str
    side: Side
    ref_price: int
    fill_price: float
    qty: int
    ts: datetime

    @property
    def bps(self) -> float:
        raw = (self.fill_price - self.ref_price) / self.ref_price * 10_000
        return raw if self.side is Side.BUY else -raw


@dataclass(frozen=True)
class SlippageStats:
    """분포 요약. 표본이 없으면 전부 None (위 docstring 참고)."""

    sample_size: int
    total_qty: int
    mean_bps: float | None = None
    median_bps: float | None = None
    p90_bps: float | None = None
    worst_bps: float | None = None
    # 수량 가중 평균. 실제로 지불한 원화 손실에 비례하는 것은 이쪽이다 —
    # 단순 평균은 1주짜리 주문과 1,000주짜리 주문을 같게 센다.
    qty_weighted_mean_bps: float | None = None


def summarize(samples: Iterable[SlippageSample]) -> SlippageStats:
    rows = list(samples)
    if not rows:
        return SlippageStats(sample_size=0, total_qty=0)

    values = sorted(sample.bps for sample in rows)
    total_qty = sum(sample.qty for sample in rows)
    weighted = (
        sum(sample.bps * sample.qty for sample in rows) / total_qty if total_qty > 0 else None
    )
    return SlippageStats(
        sample_size=len(rows),
        total_qty=total_qty,
        mean_bps=statistics.fmean(values),
        median_bps=statistics.median(values),
        p90_bps=_percentile(values, 0.90),
        worst_bps=values[-1],
        qty_weighted_mean_bps=weighted,
    )


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """가장 가까운 순위(nearest-rank). 표본이 작을 때 보간하면 실제로 겪지
    않은 값이 통계로 나온다 — 몇 건 안 되는 실전 체결에서는 그게 더 위험하다."""
    if not sorted_values:
        raise ValueError("empty")
    index = min(len(sorted_values) - 1, max(0, round(q * len(sorted_values) + 0.5) - 1))
    return sorted_values[index]


def load_live_samples(engine: Engine, *, since: date | None = None) -> list[SlippageSample]:
    """실전/모의 체결에서 표본을 만든다.

    `ref_price`가 없는 행은 **조용히 건너뛴다** — 이 컬럼이 생기기 전에
    나간 주문이거나, 봉 없이 나간 청산이다. 기준가가 없으면 계산할 것이
    없지 0이 아니다.
    """
    o = db.orders.c
    f = db.fills.c
    query = (
        sa.select(o.symbol, o.side, o.ref_price, f.price, f.qty, f.ts)
        .select_from(db.orders.join(db.fills, o.order_id == f.order_id))
        .where(o.ref_price.is_not(None), o.ref_price > 0)
        .order_by(f.ts)
    )
    if since is not None:
        query = query.where(f.ts >= datetime.combine(since, time.min))

    with engine.connect() as conn:
        return [
            SlippageSample(
                symbol=row.symbol,
                side=Side(row.side),
                ref_price=row.ref_price,
                fill_price=float(row.price),
                qty=row.qty,
                ts=row.ts,
            )
            for row in conn.execute(query)
        ]


@dataclass(frozen=True)
class SlippageReport:
    """매수·매도를 따로 본다. 매도에는 IMMEDIATE 청산이 몰려 있어 성격이
    다르다 — 급하게 파는 주문이 더 불리하게 체결되는 것이 정상이고, 한
    숫자로 합치면 그 비대칭이 사라진다."""

    overall: SlippageStats
    buys: SlippageStats
    sells: SlippageStats

    @classmethod
    def of(cls, samples: Iterable[SlippageSample]) -> SlippageReport:
        rows = list(samples)
        return cls(
            overall=summarize(rows),
            buys=summarize(s for s in rows if s.side is Side.BUY),
            sells=summarize(s for s in rows if s.side is Side.SELL),
        )

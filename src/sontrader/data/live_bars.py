"""1분봉 저장 (분봉 수집기 — 구현 계획에 번호 없음).

`adapters/live_ws.py`의 `LiveTickStream`이 만든 완성 봉을 `on_bar` 콜백에서
받아 이 모듈로 넘기면 된다(예: ``on_bar=lambda bar: live_bars.store(engine, bar)``).
`adapters/live_ticks.py`와 마찬가지로 이 모듈도 소켓·스레드를 모르는
DB 계층일 뿐이다.

## upsert인 이유

`LiveTickStream`이 재연결하면 같은 분의 틱이 다시 집계될 수 있다 — 재연결
직후 마감 처리 중이던 분이 다시 시작되거나, 서버가 최근 체결을 다시
보낼 수 있다. append만 하면 같은 (symbol, ts)에 PK 충돌로 예외가 나거나
중복이 쌓인다. `db.upsert_rows`로 최신 집계값으로 덮어써 둘 다 피한다.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.core.types import Bar
from sontrader.data import db


def store(engine: Engine, bar: Bar) -> None:
    with engine.begin() as conn:
        db.upsert_rows(
            conn,
            db.stock_candles_1m,
            [
                {
                    "symbol": bar.symbol,
                    "ts": bar.ts,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    # 우리가 틱을 직접 집계한 봉이다. 거래소 확정 봉(`rest`)과
                    # 값이 갈리므로(시간외 포함, 재연결 구멍) 출처를 남긴다 —
                    # 백테스트가 이 행을 학습하면 실전에서 재현 불가능한
                    # 청산이 나온다(`data/db.py`의 표 참고).
                    "source": "ws",
                }
            ],
            key_cols=("symbol", "ts"),
        )


def load_recent(engine: Engine, symbol: str, *, count: int) -> list[Bar]:
    """가장 최근 `count`개 봉을 시각 오름차순으로 반환 (부족하면 있는 만큼).

    `core.types.BarView.history()`와 같은 계약이다 — `engine/context.py`가
    이 함수로 실시간용 `BarView`를 조립할 수 있다(다음 슬라이스).
    """
    columns = db.stock_candles_1m.c
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                columns.ts, columns.open, columns.high, columns.low, columns.close, columns.volume
            )
            .where(columns.symbol == symbol)
            .order_by(columns.ts.desc())
            .limit(count)
        ).all()
    return [
        Bar(
            symbol=symbol,
            ts=row.ts,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
        )
        for row in reversed(rows)
    ]

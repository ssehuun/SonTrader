"""실측 슬리피지 추출기 테스트 (격차 #4).

가장 중요하게 보는 것: **표본이 없으면 숫자를 만들지 않는다.** 0.0을
반환하면 "슬리피지가 없다"로 읽혀 자리표시자(10bp)보다 나쁜 거짓말이 된다.

그 다음이 부호 규약 — 매수·매도를 한 표본으로 합치려면 **불리한 쪽이 항상
양수**여야 한다.
"""

from datetime import date, datetime

import pytest

from sontrader.apps.slippage import (
    SlippageReport,
    SlippageSample,
    load_live_samples,
    summarize,
)
from sontrader.core.types import Order, OrderStatus, OrderType, Side, Urgency
from sontrader.data import db
from sontrader.data import orders as orders_repo

NOW = datetime(2026, 8, 26, 9, 5)


def sample(side: Side, ref: int, fill: float, qty: int = 10) -> SlippageSample:
    return SlippageSample(
        symbol="005930", side=side, ref_price=ref, fill_price=fill, qty=qty, ts=NOW
    )


# --- 부호 규약 --------------------------------------------------------------


def test_buying_above_the_reference_price_is_positive_bps():
    assert sample(Side.BUY, 10_000, 10_010).bps == pytest.approx(10.0)


def test_selling_below_the_reference_price_is_also_positive_bps():
    """불리한 쪽이 양수 — 부호를 side마다 뒤집지 않으면 합칠 수 없다."""
    assert sample(Side.SELL, 10_000, 9_990).bps == pytest.approx(10.0)


def test_a_favourable_fill_is_negative_bps():
    assert sample(Side.BUY, 10_000, 9_990).bps == pytest.approx(-10.0)
    assert sample(Side.SELL, 10_000, 10_010).bps == pytest.approx(-10.0)


# --- 표본이 없을 때 ---------------------------------------------------------


def test_no_samples_yields_no_numbers_at_all():
    stats = summarize([])

    assert stats.sample_size == 0
    assert stats.mean_bps is None
    assert stats.median_bps is None
    assert stats.p90_bps is None
    assert stats.worst_bps is None
    assert stats.qty_weighted_mean_bps is None


# --- 요약 통계 --------------------------------------------------------------


def test_summary_statistics():
    stats = summarize(
        [
            sample(Side.BUY, 10_000, 10_010),  # +10bp
            sample(Side.BUY, 10_000, 10_030),  # +30bp
            sample(Side.BUY, 10_000, 10_020),  # +20bp
        ]
    )

    assert stats.sample_size == 3
    assert stats.total_qty == 30
    assert stats.mean_bps == pytest.approx(20.0)
    assert stats.median_bps == pytest.approx(20.0)
    assert stats.worst_bps == pytest.approx(30.0)


def test_quantity_weighted_mean_differs_from_the_plain_mean():
    """1주짜리와 1,000주짜리를 같게 세면 실제 손실과 어긋난다."""
    stats = summarize(
        [
            sample(Side.BUY, 10_000, 10_100, qty=1),  # +100bp, 1주
            sample(Side.BUY, 10_000, 10_000, qty=999),  # 0bp, 999주
        ]
    )

    assert stats.mean_bps == pytest.approx(50.0)
    assert stats.qty_weighted_mean_bps == pytest.approx(0.1)


def test_report_splits_buys_and_sells():
    """매도에는 IMMEDIATE 청산이 몰려 있어 성격이 다르다 — 합치면 비대칭이 사라진다."""
    report = SlippageReport.of([sample(Side.BUY, 10_000, 10_010), sample(Side.SELL, 10_000, 9_950)])

    assert report.buys.sample_size == 1
    assert report.sells.sample_size == 1
    assert report.buys.mean_bps == pytest.approx(10.0)
    assert report.sells.mean_bps == pytest.approx(50.0)
    assert report.overall.sample_size == 2


# --- DB 로딩 ---------------------------------------------------------------


def seed(db_engine, *, order_id: str, side: Side, ref_price: int | None, fill_price: int) -> None:
    order = Order(
        idempotency_key=f"005930:{side.value}:{order_id}",
        symbol="005930",
        side=side,
        qty=10,
        order_type=OrderType.MARKET,
        urgency=Urgency.NEXT_OPEN,
        ts=NOW,
        ref_price=ref_price,
    )
    orders_repo.insert(
        db_engine, order, order_id=order_id, status=OrderStatus.FILLED, created_at=NOW
    )
    with db_engine.begin() as conn:
        conn.execute(db.fills.insert().values(order_id=order_id, price=fill_price, qty=10, ts=NOW))


def test_load_live_samples_joins_orders_and_fills(db_engine):
    db.migrate(db_engine)
    seed(db_engine, order_id="o1", side=Side.BUY, ref_price=10_000, fill_price=10_020)

    [row] = load_live_samples(db_engine)

    assert row.ref_price == 10_000
    assert row.fill_price == 10_020
    assert row.bps == pytest.approx(20.0)


def test_orders_without_a_reference_price_are_skipped_not_counted_as_zero(db_engine):
    """기준가가 없으면 계산할 것이 없지 0이 아니다 — 0으로 세면 평균이 희석된다."""
    db.migrate(db_engine)
    seed(db_engine, order_id="o1", side=Side.BUY, ref_price=None, fill_price=10_020)
    seed(db_engine, order_id="o2", side=Side.BUY, ref_price=10_000, fill_price=10_020)

    samples = load_live_samples(db_engine)

    assert [s.bps for s in samples] == [pytest.approx(20.0)]


def test_since_filters_by_fill_timestamp(db_engine):
    db.migrate(db_engine)
    seed(db_engine, order_id="o1", side=Side.BUY, ref_price=10_000, fill_price=10_020)

    assert load_live_samples(db_engine, since=date(2026, 8, 26)) != []
    assert load_live_samples(db_engine, since=date(2026, 8, 27)) == []

"""스냅샷 빌더 통합 테스트 — SQLite 위에서 전체 파이프라인 검증."""

from datetime import date, datetime, timedelta

import sqlalchemy as sa

from sontrader.data import db, universe

AS_OF = date(2026, 8, 3)


def seed_master(engine, symbol, *, market="KOSPI", group_code="ST", op_profit=1000):
    with engine.begin() as conn:
        conn.execute(
            db.symbol_master.insert().values(
                symbol=symbol,
                name=f"종목{symbol}",
                market=market,
                group_code=group_code,
                cap_scale_code="1",
                low_liquidity_yn="N",
                spac_yn="N",
                pref_share_code="0",
                base_price=10000,
                suspended_yn="N",
                liquidation_yn="N",
                managed_yn="N",
                market_warning_code="00",
                unfaithful_yn="N",
                prev_volume=100000,
                op_profit=op_profit,
                market_cap=10000,
                updated_at=datetime(2026, 8, 3, 8, 0),
            )
        )


def seed_candles(engine, symbol, closes, *, end=AS_OF, trade_value=2_000_000_000):
    """closes를 end에서 거꾸로 평일에만 배치한다 (마지막 값이 end 근처)."""
    rows = []
    cursor = end
    for close in reversed(closes):
        while cursor.weekday() >= 5:
            cursor -= timedelta(days=1)
        rows.append(
            {
                "symbol": symbol,
                "date": cursor,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1000,
                "trade_value": trade_value,
            }
        )
        cursor -= timedelta(days=1)
    with engine.begin() as conn:
        conn.execute(db.stock_candles_1d.insert(), rows)


def flat_then_up(final_return, bars=7):
    """룩백 구간 시작가 100, 마지막에 수익률 final_return이 되는 시계열."""
    closes = [100.0] * (bars - 2)
    closes += [100.0 * (1 + final_return)] * 2  # skip=1이 건너뛸 마지막 봉 포함
    return closes


def build(engine, **kwargs):
    params = dict(as_of=AS_OF, lookback=5, skip=1, min_avg_trade_value=1_000_000_000)
    params.update(kwargs)
    return universe.build_snapshot(engine, **params)


def stored_snapshot(engine, as_of=AS_OF):
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(db.watchlist_snapshots)
            .where(db.watchlist_snapshots.c.date == as_of)
            .order_by(db.watchlist_snapshots.c.rank)
        ).all()
    return [(r.symbol, r.rank) for r in rows]


def test_snapshot_ranks_by_momentum_and_persists(db_engine):
    db.migrate(db_engine)
    seed_master(db_engine, "000001")
    seed_master(db_engine, "000002")
    seed_candles(db_engine, "000001", flat_then_up(0.10))
    seed_candles(db_engine, "000002", flat_then_up(0.30))

    result = build(db_engine, enter_rank=2, exit_rank=3)

    assert result.candidates == 2
    assert result.scored == 2
    assert stored_snapshot(db_engine) == [("000002", 1), ("000001", 2)]


def test_same_day_rerun_is_identical(db_engine):
    db.migrate(db_engine)
    seed_master(db_engine, "000001")
    seed_candles(db_engine, "000001", flat_then_up(0.10))

    build(db_engine, enter_rank=2, exit_rank=3)
    first = stored_snapshot(db_engine)
    build(db_engine, enter_rank=2, exit_rank=3)

    assert stored_snapshot(db_engine) == first  # 같은 날 재실행 → 동일 결과


def test_filtered_and_short_history_symbols_are_excluded(db_engine):
    db.migrate(db_engine)
    seed_master(db_engine, "000001")
    seed_master(db_engine, "000002", group_code="EF")  # ETF — 마스터 필터 탈락
    seed_master(db_engine, "000003")  # 이력 부족 (신규 상장)
    seed_master(db_engine, "000004")  # 저유동성 — 거래대금 미달
    seed_candles(db_engine, "000001", flat_then_up(0.10))
    seed_candles(db_engine, "000003", flat_then_up(0.50, bars=3))  # lookback+1 미만
    seed_candles(db_engine, "000004", flat_then_up(0.50), trade_value=100)

    result = build(db_engine, enter_rank=2, exit_rank=3)

    assert result.candidates == 3  # ETF는 후보에서 제외
    assert result.scored == 1
    assert [s for s, _ in stored_snapshot(db_engine)] == ["000001"]


def test_no_candles_raises_universe_error(db_engine):
    import pytest

    db.migrate(db_engine)
    seed_master(db_engine, "000001")

    with pytest.raises(universe.UniverseError):
        build(db_engine)


def test_snapshot_is_dated_by_last_trading_day_not_wall_clock(db_engine):
    # 주말/휴장일에 실행해도 스냅샷은 데이터의 마지막 거래일로 기록된다.
    db.migrate(db_engine)
    seed_master(db_engine, "000001")
    seed_candles(db_engine, "000001", flat_then_up(0.10))  # 마지막 봉 = AS_OF(월요일)

    result = build(db_engine, as_of=AS_OF + timedelta(days=1), enter_rank=2, exit_rank=3)

    assert result.as_of == AS_OF  # 데이터 기준일
    assert result.requested == AS_OF + timedelta(days=1)
    assert stored_snapshot(db_engine, as_of=AS_OF) != []
    assert stored_snapshot(db_engine, as_of=AS_OF + timedelta(days=1)) == []


def test_stale_series_is_excluded_from_scoring(db_engine):
    # 마지막 봉이 기준일보다 한참 오래된 종목(상폐·수집 실패)은 점수에서 제외.
    db.migrate(db_engine)
    seed_master(db_engine, "000001")
    seed_master(db_engine, "000002")
    seed_candles(db_engine, "000001", flat_then_up(0.10))
    seed_candles(db_engine, "000002", flat_then_up(0.90), end=AS_OF - timedelta(days=30))

    result = build(db_engine, enter_rank=2, exit_rank=3)

    assert result.scored == 1
    assert [s for s, _ in stored_snapshot(db_engine)] == ["000001"]


def test_hysteresis_keeps_previous_member_between_50_and_70(db_engine):
    db.migrate(db_engine)
    # 어제 스냅샷에 000009가 있었다고 기록한다.
    with db_engine.begin() as conn:
        conn.execute(
            db.watchlist_snapshots.insert().values(
                date=AS_OF - timedelta(days=1), symbol="000009", score=0.5, rank=1
            )
        )
    # 오늘 점수는 000009가 3위 (enter_rank=2 밖, exit_rank=3 안).
    for i, ret in ((1, 0.30), (2, 0.20), (9, 0.10)):
        symbol = f"00000{i}"
        seed_master(db_engine, symbol)
        seed_candles(db_engine, symbol, flat_then_up(ret))

    result = build(db_engine, enter_rank=2, exit_rank=3)

    stored = stored_snapshot(db_engine)
    assert ("000009", 3) in stored  # 기존 멤버라 3위로 생존
    assert len(result.entries) == 3


# --- UniverseScope (과거 소급 생성용 필터) ------------------------------------

STRUCTURAL = dict(scope=universe.UniverseScope.STRUCTURAL)


def test_structural_scope_keeps_symbols_that_are_managed_today(db_engine):
    """오늘 관리종목이어도 과거 스냅샷 후보에는 남는다.

    오늘의 상태 플래그를 과거에 적용하면 "지금까지 살아남은 종목"만 남아
    생존 편향이 확정된다 — 과거 시점에는 멀쩡했을 수 있다.
    """
    db.migrate(db_engine)
    seed_master(db_engine, "005930")
    seed_candles(db_engine, "005930", flat_then_up(0.5))
    with db_engine.begin() as conn:
        conn.execute(
            sa.update(db.symbol_master)
            .where(db.symbol_master.c.symbol == "005930")
            .values(managed_yn="Y", listing_date=date(2010, 1, 4))
        )

    assert build(db_engine).candidates == 0  # 기본 모드는 제외한다
    assert build(db_engine, **STRUCTURAL).candidates == 1


def test_structural_scope_still_excludes_structural_kinds(db_engine):
    """구조적 속성(ETF·우선주·SPAC)은 여전히 제외한다 — 시간이 지나도 안 변한다."""
    db.migrate(db_engine)
    for symbol, overrides in [
        ("069500", {"group_code": "EF"}),
        ("005935", {"pref_share_code": "1"}),
        ("123456", {"spac_yn": "Y"}),
        ("005930", {}),
    ]:
        seed_master(db_engine, symbol)
        seed_candles(db_engine, symbol, flat_then_up(0.5))
        with db_engine.begin() as conn:
            conn.execute(
                sa.update(db.symbol_master)
                .where(db.symbol_master.c.symbol == symbol)
                .values(listing_date=date(2010, 1, 4), **overrides)
            )

    assert build(db_engine, **STRUCTURAL).candidates == 1


def test_structural_scope_judges_listing_age_against_as_of(db_engine):
    """상장일 판정이 as_of 기준이라 오늘 기준보다 point-in-time에 가깝다."""
    db.migrate(db_engine)
    seed_master(db_engine, "005930")
    seed_candles(db_engine, "005930", flat_then_up(0.5))
    # as_of(2026-08-03) 기준 400일이 안 된 상장일
    with db_engine.begin() as conn:
        conn.execute(
            sa.update(db.symbol_master)
            .where(db.symbol_master.c.symbol == "005930")
            .values(listing_date=AS_OF - timedelta(days=399))
        )

    assert build(db_engine, **STRUCTURAL).candidates == 0


def test_tradeable_scope_remains_the_default(db_engine):
    """기본 동작이 조용히 바뀌지 않았는지."""
    db.migrate(db_engine)
    seed_master(db_engine, "005930")
    seed_candles(db_engine, "005930", flat_then_up(0.5))

    # listing_date 없이도 기본 모드는 동작한다 (is_tradeable은 날짜를 안 본다)
    assert build(db_engine).candidates == 1

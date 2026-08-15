"""core 순수 함수 테스트 — 모멘텀, 히스테리시스, 방어 필터."""

import dataclasses
from datetime import date, timedelta

import pytest

from sontrader.core.filters import (
    SecurityInfo,
    StructuralInfo,
    is_collectable,
    is_tradeable,
)
from sontrader.core.momentum import momentum_score
from sontrader.core.watchlist import build_watchlist

# --- momentum --------------------------------------------------------------


def test_momentum_is_return_between_lookback_and_skip():
    closes = [100.0, 110.0, 120.0, 130.0, 140.0, 150.0]
    # lookback=5 → 기준 100, skip=1 → 최근 140. 점수 = 140/100 - 1
    assert momentum_score(closes, lookback=5, skip=1) == pytest.approx(0.4)


def test_momentum_requires_full_history():
    assert momentum_score([100.0] * 252, lookback=252, skip=21) is None  # 253개 필요
    assert momentum_score([100.0] * 253, lookback=252, skip=21) == pytest.approx(0.0)


def test_momentum_rejects_bad_inputs():
    assert momentum_score([0.0, 100.0, 110.0], lookback=2, skip=1) is None  # 기준가 0
    with pytest.raises(ValueError):
        momentum_score([1.0] * 10, lookback=5, skip=5)  # skip >= lookback


# --- watchlist (히스테리시스) ------------------------------------------------


def scores_for(count: int) -> dict[str, float]:
    # 점수는 순위 역순으로: sym001이 1위가 되도록.
    return {f"sym{i:03d}": float(count - i) for i in range(1, count + 1)}


def test_new_symbols_enter_only_within_enter_rank():
    watchlist = build_watchlist(scores_for(100), previous=set(), enter_rank=50, exit_rank=70)

    assert len(watchlist) == 50
    assert watchlist[0].symbol == "sym001"
    assert watchlist[-1].rank == 50


def test_existing_symbol_survives_between_enter_and_exit_rank():
    # sym060은 60위: 신규라면 탈락하지만 기존 멤버라 유지된다.
    watchlist = build_watchlist(scores_for(100), previous={"sym060"}, enter_rank=50, exit_rank=70)

    symbols = {e.symbol for e in watchlist}
    assert "sym060" in symbols
    assert len(watchlist) == 51


def test_existing_symbol_beyond_exit_rank_drops_out():
    watchlist = build_watchlist(scores_for(100), previous={"sym071"}, enter_rank=50, exit_rank=70)

    assert "sym071" not in {e.symbol for e in watchlist}


def test_ties_break_deterministically_by_symbol():
    scores = {"bbb": 1.0, "aaa": 1.0, "ccc": 1.0}
    watchlist = build_watchlist(scores, previous=set(), enter_rank=2, exit_rank=2)

    assert [e.symbol for e in watchlist] == ["aaa", "bbb"]


# --- filters ----------------------------------------------------------------


def make_info(**overrides) -> SecurityInfo:
    base = dict(
        symbol="005930",
        name="삼성전자",
        market="KOSPI",
        group_code="ST",
        cap_scale_code="1",
        low_liquidity_yn="N",
        spac_yn="N",
        pref_share_code="0",
        base_price=71000,
        suspended_yn="N",
        liquidation_yn="N",
        managed_yn="N",
        market_warning_code="00",
        unfaithful_yn="N",
        op_profit=650000,
    )
    return SecurityInfo(**{**base, **overrides})


def test_healthy_common_stock_passes():
    assert is_tradeable(make_info()) is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"group_code": "EF"},  # ETF
        {"cap_scale_code": "0"},  # 시총규모 미분류
        {"suspended_yn": "Y"},
        {"liquidation_yn": "Y"},
        {"managed_yn": "Y"},
        {"low_liquidity_yn": "Y"},
        {"spac_yn": "Y"},
        {"unfaithful_yn": "Y"},
        {"pref_share_code": "1"},  # 우선주
        {"market_warning_code": "01"},  # 시장경고
        {"base_price": 900},  # 동전주
        {"base_price": None},
        {"name": "OO스팩5호"},
        {"market": "KOSPI", "op_profit": -100},  # 유가는 영업이익 양수 요구
        {"market": "KOSPI", "op_profit": None},
    ],
)
def test_defensive_filters_reject(overrides):
    assert is_tradeable(make_info(**overrides)) is False


def test_kosdaq_does_not_require_positive_op_profit():
    info = make_info(market="KOSDAQ", op_profit=-100)
    assert is_tradeable(info) is True


def test_null_safety_flags_fail_closed():
    # 마스터가 아직 채워지지 않은 행(NULL 플래그)은 통과가 아니라 제외.
    assert is_tradeable(make_info(suspended_yn=None)) is False
    assert is_tradeable(make_info(managed_yn=None)) is False
    assert is_tradeable(make_info(liquidation_yn=None)) is False


# --- is_collectable (구조적 필터) --------------------------------------------

TODAY = date(2026, 8, 16)


def structural(**overrides):
    base = dict(
        symbol="005930",
        name="삼성전자",
        group_code="ST",
        pref_share_code="0",
        spac_yn="N",
        listing_date=date(1975, 6, 11),
    )
    return StructuralInfo(**{**base, **overrides})


def test_collectable_accepts_ordinary_long_listed_stock():
    assert is_collectable(structural(), today=TODAY) is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"group_code": "EF"},  # ETF
        {"group_code": "EN"},  # ETN
        {"pref_share_code": "1"},  # 우선주
        {"spac_yn": "Y"},
        {"name": "교보18호스팩"},
        {"listing_date": None},  # 상장일 불명 → fail-closed
        {"listing_date": date(2026, 3, 1)},  # 상장 400일 미만
    ],
)
def test_collectable_rejects_structural_exclusions(overrides):
    assert is_collectable(structural(**overrides), today=TODAY) is False


@pytest.mark.parametrize("days,expected", [(400, True), (399, False)])
def test_collectable_listing_age_boundary(days, expected):
    info = structural(listing_date=TODAY - timedelta(days=days))
    assert is_collectable(info, today=TODAY) is expected


def test_collectable_ignores_time_varying_state():
    """관리종목·거래정지 같은 시변 상태는 StructuralInfo에 아예 없다.

    수집 단계에서 그것들로 거르면 백테스트에 생존 편향이 들어가기 때문이다
    (filters.py 모듈 독스트링 참고). 타입으로 실수를 막는다.
    """
    fields = {f.name for f in dataclasses.fields(StructuralInfo)}
    assert fields.isdisjoint(
        {"managed_yn", "suspended_yn", "market_warning_code", "op_profit", "cap_scale_code"}
    )
